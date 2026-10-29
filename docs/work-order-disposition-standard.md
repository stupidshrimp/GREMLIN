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
| **Developer > Code lists** (administrators): every code list as a workbook to download, edit and upload | `/developer/code-lists` — `templates/developer_code_lists.html`, `static/js/developer_code_lists.js`; `StandardDispositionService.export_code_lists_excel` / `import_code_lists_excel` (tested in `tests/test_code_list_workbook.py`) |
| **Disposition** (editors) | `/life-data-analysis/disposition` — `templates/disposition.html`, `static/js/disposition.js` |
| **Perform an Analysis** (any account that may see Life Data Analysis) | `/life-data-analysis/perform-analysis` — `templates/perform_analysis.html`, `static/js/life_data_analysis.js` |
| The disposition form both pages use | `static/js/disposition_form.js` (the Disposition page's right-hand window, and the editor that opens over Perform an Analysis from any task ID in its tables) |
| **The disposition helper** (Auto-disposition): reading a work order, suggesting its disposition, batch runs and their undo, the accuracy report | `services/disposition_helper_text.py` (words, the thesaurus, negation, spelling) and `services/disposition_helper.py` (the index, voting, calibration, runs), `services/disposition_helper_embedding.py` (meaning matching: the model, its vector cache); saves and undo in `StandardDispositionService.save_auto_dispositions` / `undo_helper_run`; `tools/fetch_embedding_model.py` puts the model in place. Tested in `tests/test_disposition_helper.py` and `tests/test_disposition_helper_meaning.py` on the synthetic plant of `tests/disposition_helper_corpus.py` |

There is one page for each job. **Disposition** shows an asset's work orders on
the left and the one picked on the right, in the disposition form; Save & next
moves down the list. Each work order opens in Limble from its card (**Open in
Limble**, a new tab at `LIMBLE_APP_URL/taskList?taskID=…`, as the PM calendar's
links do) and from the arrow beside its task number in the list, and the list's
**Last edited by** column says who saved the disposition it has now (an
engineer, "Migration", or "Disposition helper (who ran it)"), sortable and
filterable like the rest. A bar over the two (sticky below the top bar) switches the
view: **Side by side**; **Disposition**, the form alone across the page with the
work order's record on the left (kept in view as the page scrolls) and its
fields on the right, the work order's place in the list and the ones before and
after it in the bar, across pages too; or **Work orders**, the list alone across
the page, where picking a work order opens it in the Disposition view. The
list holds still while it is worked through: opened (or its Show, search,
filters or sort changed), it comes with the order of every work order it holds,
and each later page is read as that order's slice, so saving work orders out of
"Only not reviewed" never makes the next page skip any. Save & next on a page's
last work order goes on to the next page's first. The
browser remembers the view picked on the bar. The change is a view transition
where the browser has one (the card that stays grows or shrinks into place, the
others slide out and in), a short rise-in otherwise, and none for anyone who has
asked their system for less motion. Every code field on the form (component, part, mechanism,
mode, action, cause, and the renewed items) is a search: typing any part of a
code, its name or its description lists the codes that hold every word typed,
best first, and when nothing on the list is what the work order needs an editor
adds it from the field itself (see *Adding a code from the form* below). Every column of the list sorts and filters from the ▾ on its
header, server-side, so both reach every row rather than the page on screen. The
picked work order shows its facts in a labelled grid (asset; created, started and
completed on the plant's clock; downtime; record class; category), then its
request title and description, its failure narrative and its completion notes,
each under its own heading, with an empty part marked rather than left out. The
same records can be dispositioned offline in a workbook (Download Excel / Upload
Excel), which holds the rows the list shows, and the code lists are edited from
there.
**Perform an Analysis** analyses one population: an asset, narrowed in the
selector (the sub-asset list shows the asset, then each sub-asset under its
parent) and by date. What the selector offers depends on the Analysis Type:

* **Weibull Analysis** narrows by sub-asset, component, part and mechanism only,
  plus the induced-failures switch (item 13 below). Its Pareto has one bar per
  sub-asset + component (or part) + mechanism among the population's failures, ranked by
  count or downtime; clicking a bar fits exactly that (saved for an editor, a
  preview otherwise) without narrowing the population or changing the Pareto.
  When every bar is on one place, as on an asset with no sub-assets, the names
  start at the component.
* **Triage Analysis** (formerly Failure Mode Trend Analysis), **Downtime Driver**,
  **PM Effectiveness** and **Repeat Fix Rate** narrow by all seven coded fields —
  sub-asset, component, part, mechanism, mode, cause and action — and by
  disposition category. Their Pareto is grouped by the dimension chosen (sub-asset
  at first, or component for an asset with no sub-assets), and a bar or matrix
  cell drills down sub-asset → component → part (when the component's work orders
  name any) → mechanism. A breadcrumb right above the bars
  shows the way the drilling came: each step is a click back to it, with Back
  for the last drill and Start over.

Sub-asset, component, part and mechanism carry across a switch between the two; the
mode, cause, action and categories ticked for the second group wait there while
the Weibull shows.

Both pages follow Life Data Analysis's access rules: reads for anyone who may see
Life Data Analysis, every write (a disposition, a code, a saved Weibull run, an
adjustment, a numbered report) for editors, recorded against the account.
Dispositioning is Facilities' work: the Disposition page is for editors and
administrators recorded as Facilities or as all departments, and Operations,
Maintenance and Operations & Maintenance accounts are kept out of it, and of
every Life Data Analysis endpoint, whatever their role. A
Weibull fit can be previewed by any reader; saving it needs an editor.

### The analyses, on the population

* **Readiness** counts the work orders in reach: dispositioned, not reviewed,
  migrated and still to confirm, failures and PM resets in the population, and
  failures or PM resets with no component (which no life can use).
* **Pareto** — one bar per value of the dimension chosen (sub-asset by default,
  where the asset's own work orders are one bar; component, part, mechanism, mode,
  cause, action, category, detection method), ranked by count or downtime, each
  bar split by failure cause. Work orders not coded on that dimension make no bar
  (nor a value in the trend's summary cards); the chart says how many were left out.
  On the Weibull, one bar per sub-asset + component + mechanism among the
  failures instead (`view: "weibull"` on the summary request).
* **Triage** — the population's work orders per month (continuous axis),
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
* **Repeat fix rate** — item by item (sub-asset + component, or + part), a failure is a
  repeat when it is found within the window (24 scheduled hours by default) of the
  previous failure's repair being finished, on that sub-asset's Weibull schedule.
* **Weibull** — the renewal-stream fit below, shown with its population
  definition, validity warnings, the items pooled, the sensitivity fit, the
  charts, the interpretation summary, the lives and the event processing table.
  Nothing is fitted, or opened, until asked: picking an asset or changing the
  population leaves the results empty. A Pareto bar fits that one sub-asset,
  component (or part) and mechanism; Perform Analysis (editors) fits and saves the
  population ticked, Preview the fit (viewers) fits it without saving; a saved
  fit opens from the rankings. Adjusted beta and eta are kept beside a saved run, with the
  reason. **Generate Weibull Report** writes the numbered Word report of a saved
  run with its population definition, items pooled and warnings.
* **Rankings** — the latest saved fit of each one-item population (one component, or one part) the
  asset reaches, with at least 5 failure lives: by beta, and by the chance that
  one of its items in service fails in the next N weeks
  (1 − Π R(tᵢ + Δᵢ) / R(tᵢ) over the items still in service at the run's cutoff).

## Data model

* **`disposition_code`** — the six code lists (component, part, failure
  mechanism, failure mode, failure cause, action) as configuration. Each list is seeded from
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
  Administrators can also keep the lists in Excel, from **Developer > Code
  lists**: the download is one row per code (retired ones too), with the
  columns an upload reads highlighted, empty highlighted rows to add codes in,
  dropdowns for the list, the meaning, yes/no and the default cause, and how many
  work orders use each code. The upload adds and changes codes through the same
  checks as the Code lists dialog, all or none (one refused row refuses the
  upload, naming its row), leaves a row that already reads as its code alone, and
  never deletes: a code left out of the workbook is kept. An empty sort order puts
  a new code at the end of its list; an empty `is_active` or `renews_item` keeps
  what the code had.
  What a code means *to the engine* is an attribute, not its spelling, so the team
  can rename and extend lists freely:
  * `renews_item` on actions (seeded on REPLACE, REPAIR, CLEAN_SERVICE);
  * `default_cause_code` on mechanisms (spec §3.2's "default cause");
  * `semantic`: `UNKNOWN` (mechanism UNK, cause Unknown),
    `INSPECTION_FINDING` (mode DEG → detection method Inspection),
    `NO_FAULT_FOUND` (mode NFF), `INDUCED` (causes Installation and Maintenance
    error).

  **Components and parts are a name and a description**, nothing else: a plant
  has hundreds, so they carry no ISO reference, meaning or order, and list by
  name. A new one's code is made from its name (each word's letters and digits,
  capitalised and run together: *Air compressor* → `AirCompressor`, with a
  number added if that code is taken), so adding one is typing a name; a name
  already on the list, in any capitals, is refused as the same item twice. The
  component is the maintainable item (a compressor, a pump); the part is the
  part within it that failed or was replaced (a filter, an O-ring). Parts are
  one list shared by every component. Components named Unknown or Other read as
  no component to the helper. The code-list workbook takes a component or part
  row by its name alone and ignores the other columns on it.
* **`wo_disposition`** — one row per decision, with history (`is_current`), like
  `event_disposition`. Fields are spec §2's plus `part_code`, the part within the
  component (optional, and only with a component); renewed items are stored as a
  JSON list of component codes; the linked WO as a `mapped_record_id` (entered as
  a task ID).
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

### Parts, in the life engine

A part is its own item. A renewal stream is keyed by sub-asset, component and
part, the part blank for the whole component, so *Compressor › Filter* has
lives of its own apart from *Compressor*:

* A failure that names a part ends (and, when its action renews, renews) that
  part's life, not the component's.
* Renewing a whole component renews its parts with it: a failure of the
  compressor whose action renews it, or a PM_RESET or renewed item naming the
  compressor, ends each of its parts' running lives as a suspension ("renewed
  with the whole component it is part of") and starts them afresh. Replacing a
  filter does not renew the compressor.
* A PM_RESET that names a part renews that part; with a part it needs no
  renewed items, though it may list components it also renewed.
* A population with parts ticked is those parts' streams (in one component, or
  across every component the population holds); with none ticked it is the
  whole components only, so a Weibull of *Compressor* is the compressor as a
  whole, and fits saved before parts existed read exactly as they did.

### Adding a code from the form

An editor adds a code from the field that needs it: typing what is not on the
list offers **Add "…" as a new component** (prominently when nothing matches,
under the matches otherwise), which opens a small panel under the field. A
component or part asks only a name and a description; any other code its code
as well, and a mechanism its default cause, an action whether it renews (what a
code means and where it sorts stay in the Code lists dialog). The code is added
to the list for every work order and set on the field, unsaved like any other
change. A code meant to be new never changes one already listed
(`is_new`): its code being taken is refused, naming the code that has it.

Before it is added, the **disposition helper looks for one already on the list
that it may simply be** (`POST /life-data-analysis/api/code-lists/similar`, also
asked by the Code lists dialog's Add): the same name in other capitals or
punctuation, a near spelling (one slip in a word of 4–7 letters, two in a longer
one: *Fliter*, *Compresor*), shared words weighted by how rare they are in the
list, the new name being one of a code's names or the thesaurus's phrases for
it (*air compressor* in "HVAC / compressor / chiller"), and, when the local
model is on, much the same meaning in other words. Up to five come back best
first, each with why; retired ones too, marked. They are shown as the name is
typed, each with **Use this**; with any showing, the button reads **Add it
anyway**, and an Add pressed before they were shown shows them first.

The check runs for **every list** (mechanism, mode, cause and action as much as
component and part), wherever a code is added:

* **From a field on the form, and from the Code lists dialog**, as above.
* **On the server**, for any code that would be added however it came
  (`StandardDispositionService.put_code`): one the helper's wording check finds
  like a listed code is refused with `409` and the codes it may be
  (`SimilarCodesExist`), unless it comes with `confirmed_new` — what the two
  pages send once the look-alikes have been shown and it is added anyway. So no
  page, old tab or script adds a look-alike unseen.
* **Across the two levels.** A new part that is a listed component under another
  spelling ("Electric motors" for the component Electric motor), or a new
  component that is a listed part, is asked about too: the form offers **Use as
  the component** for a part that is one. A part merely sharing a word with a
  component ("Compressor filter") is what a part is, and is not asked about.
* **In the code-list workbook** (Developer > Code lists), which can't ask "add it
  anyway": a new code that is a listed one under another spelling, word order or
  plural (a match of 0.85 or more: the same name, a slip of the keys, the same
  words), or a part that is a listed component, refuses its row, saying the
  dialog can add it anyway if it really is different. Each new row is also read
  against the new rows above it, so two spellings of one new code are caught. A
  new code merely like a listed one is added and named after the upload.

## Keeping it fast

Measured on a plant-sized database (43,000 work orders, 3,098 dispositioned on
4002), each view of Perform an Analysis now answers in 15-85 ms where it took
400-650 ms, and drawing a view no longer holds the page up for more than about
0.1 s, where it froze for up to 0.6 s. What does it:

* **The population is read once per database state.** Every view starts by
  reading the asset's records (`PopulationAnalysisService._load`); that result
  is kept, per asset and location reach, until GREMLIN.db changes. Whether it
  has changed is `LifeDataService.change_token`: SQLite's file change counter
  (header bytes 24-27, kept by the rollback journal every write here uses) with
  the file's size and modification time, read straight from the file -- one
  small read, nothing held open, and any process's commit moves it. It is read
  before a transaction's first query, so what is kept under it is never older
  than it says. The first view after a save reads the population again
  (about 250 ms); the rest reuse it.
* **The asset tree is kept by the same token** (`AssetTree.load(token=...)`),
  so telling whether it changed no longer reads every work order's asset
  columns; when it must, an index on `TRIM(asset_number)` and those columns
  (`idx_mapped_cmms_asset_tree`) answers without the wide rows. A population's
  records are found through that index and `idx_wo_disposition_current_sub_asset`
  rather than a scan of every work order.
* **Life hours are summed, not walked.** `_scheduled_life_hours` adds the days
  wholly inside a life from per-zone running totals of each plant day's real
  length (`_PlantDays`), so a life of years costs what a life of hours does.
  The day-by-day walk is kept as the reference (and for a zone whose midnights
  misbehave); the two agree to within floating-point rounding across zones
  with half-hour, midnight and skipped-day clock changes
  (`tests/test_analysis_speed.py`).
* **Dates read directly.** A stored date with a time goes straight to ISO
  parsing and an ISO date straight to the plant's midnight, instead of failing
  through each date-only format first.
* **Long tables draw a batch at a time.** The work orders behind a trend, the
  PMs and the repeat pairs draw their first 150 rows at once and the rest as the
  table is scrolled toward its end (or from Show all); a column's sort or filter
  draws every row first. An off-screen table is not laid out until it is
  scrolled near (`content-visibility: auto` on `.lda-table-scroll`).

## The disposition helper (Auto-disposition)

The **Auto-disposition** switch on the Disposition page turns on a helper that
reads each work order opened and fills in its disposition from the code lists,
for the engineer to check and save. It is not part of the spec; it makes the
spec's fields quicker to fill, and never changes what they mean.

**How it reads a work order.** Two sources of evidence, for every field:

* *The work orders already dispositioned.* Every work order is indexed by its
  words (stemmed, so "leaking" and "leaked" meet; a misspelling one edit away
  from a word the plant uses is read as that word), its word pairs, and the
  concepts read off it (below), weighted TF-IDF. The 25 most similar
  dispositioned work orders vote for their codes, each by its similarity
  squared; one on the same asset counts 1.5 times, one carried over by the
  migration 0.6 times, and one in another category 0.4 times for the coded
  fields. This is how the plant's own way of writing is learnt.
* *What the work order says outright.* Every name segment of every active code
  is a phrase for it ("Filter / strainer / sieve" is three), plus a built-in
  thesaurus of maintenance wording for the seed codes ("clogged", "plugged",
  "restricted" for PLUG; "won't light", "no ignition" for FTS; "swapped",
  "changed out" for REPLACE; "no fault found", "tested OK" for the mode that
  means no fault found). A phrase in the same clause after "no" or "not" counts
  against its code ("no leaks found"). Each part of the work order counts for
  what it is evidence of: the action taken for the action, the condition found
  and cause for the mechanism, the request for the mode. Words in a code's
  description count a little too, so the team can teach the helper a word by
  writing it into a description. "Tested OK" after a repair is read as the check
  after the fix, not as no fault found.

The two are blended by how much of each there is, so a work order like none
seen before is read on its wording alone and a terse one on its neighbours. The
**sub-asset** is read off its number ("S09"), its name ("dry-off oven"), or the
asset the work order was logged against, and those outweigh the votes, which
are similar for what failed more than for where. The **category** also reads
the record class (a PM leans to PM_RESET). The helper never proposes HELD or
DUPLICATE_MISFILED: it points out a likely duplicate (same asset, within 3 days,
reading much the same) and leaves the call to the engineer. It never proposes no
fault found for a FAILURE, overrides the default cause only when the work order
names another (writing the note the override needs), lists for a PM_RESET the
items the similar PM resets renewed, and marks the evidence EXPLICIT when the
work order names the component and its condition, INFERRED otherwise.

**How sure it is.** Each field's confidence is calibrated: the helper
dispositions up to 400 work orders engineers have decided as if they were new
(each without its own decision), and fits, field by field, how often a
suggestion that scored so was right (isotonic, shrunk towards a coin toss so a
handful of lucky ones never read as certain). A whole disposition is as sure as
how often past ones whose least sure field scored the same were entirely right,
and never surer than that field. Below 40 replayed work orders the helper is
uncalibrated: it still suggests, with its scores discounted and marked as
estimates, but may not disposition on its own. The calibration is refitted when
the engineers' decisions grow by a tenth, in the background after a suggestion
(a batch run and the report wait for it).

**On the page.** With the switch on (remembered per browser), a work order
nobody has decided (or only the migration or the helper has) is filled in where
the helper is at least 50% sure; an engineer's own disposition is never filled
over. Beside each field is how sure the helper is, or its value as a button when
the field holds something else; above the fields, how sure it is overall, why
(the phrases it read, the votes, the alternatives), and the most similar
dispositioned work orders. What it filled is the form's starting point, so
moving on without saving asks nothing. A save made with a suggestion open keeps
the suggestion (`wo_disposition.suggestion_json`), which is how the helper is
measured in use.

**Auto-disposition the backlog** (editors) reads the list's not-reviewed work
orders, under its search and column filters, newest first and at most 1,000 a
run, and previews what it would do at the confidence chosen (60-99%, 90% to
start). Running it saves those that reach it through the standard's own checks,
as `source = 'AUTO'`, `decided_by = 'Disposition helper (<who ran it>)'`, with a
review flag naming the confidence, under one `disposition_helper_run`. A work
order dispositioned meanwhile is left alone, and so is one the standard refuses.
Show: **Auto-dispositioned (to confirm)** lists them; opening one and saving it
confirms it (it becomes an engineer's disposition). **Undo** on a run puts every
one of its work orders not confirmed since back to what it had before the run
(the run's rows stay in the history, not current).

**What the analysis does with them.** An unconfirmed auto-disposition counts as
the disposition it is, but Perform an Analysis counts them in its readiness
("Auto-dispositioned, still to confirm") and a Weibull fit that reads any warns
(`AUTO_DISPOSITIONS`). The helper never learns from its own unconfirmed
dispositions.

**How accurate is it?** replays the decided work orders (of the asset picked, or
the plant) and reports, field by field, how often it is right overall and at
each confidence, and for whole dispositions how many a run at each setting
would save and how many of those it gets entirely right. The confidences in
that report come from a calibration fitted on the other half of the work
orders, never on the ones being graded. It also reports the helper in use: how
often a suggested value was kept on a save, and how many auto-dispositions an
engineer confirmed unchanged.

**Meaning matching (optional).** Word matching finds work orders that use the
same words or thesaurus concepts; "burner won't light" and "no flame on startup"
share neither. With meaning matching on, a small embedding model,
`sentence-transformers/all-MiniLM-L6-v2` (Apache-2.0, pinned to revision
`c9745ed1`), reads every work order's title, request, narrative and notes into
384 numbers that stand for what it means, and work orders that say the same
thing in other words are found too. It runs on the server's CPU through ONNX
Runtime; no text leaves the plant.

* *Where it counts.* Similar work orders are found by wording (TF-IDF) and by
  meaning, pooled, and scored 80% on wording and 20% on meaning (the model's
  similarity counts only above 0.2, where unrelated English sits). That
  similarity finds the votes for the **component, mechanism, mode and action**.
  The **category** and the **sub-asset** are still voted on by wording alone: a
  check of the oven's intake screen and a clogged intake screen mean nearly the
  same and are different kinds of event, and where is in names and numbers.
  More weight on meaning made the synthetic plant's dispositions worse, by
  finding work orders about the same part but a different fault.
* *What it adds, measured.* On the synthetic plant with its backlog written in
  wording no dispositioned work order used ("no flame at the dry-off oven on
  startup", "intake screen caked with dust"), whole dispositions entirely right
  went from 76.3% to 80.6% (component 78.5% to 82.8%, action 91.4% to 95.2%);
  with the backlog worded like the past, nothing got worse (92.5% to 93.0%).
  The accuracy report shows the same comparison on the plant's own records:
  with meaning matching on, every figure has a "words only" one beside it.
* *Reading the work orders.* Each work order's vector is kept, by a hash of the
  text it was read from, in `GREMLIN.embeddings.db` beside GREMLIN.db (not in
  GREMLIN.db: it is derived data, and writing it there would move the change
  token every cache above is keyed on). Work orders not read yet are read in the
  background and matched by their words until they are; the helper bar says how
  far it has got. The calibration is refitted once all are read.
* *Setting it up.* `python -m pip install -r requirements.txt` (adds `numpy`,
  `onnxruntime`, `tokenizers`), then `python tools/fetch_embedding_model.py`,
  which downloads the two model files (about 90 MB) from Hugging Face into
  `embedding-model/all-MiniLM-L6-v2` beside GREMLIN.db, checks each against its
  SHA-256, and then checks GREMLIN loads it. A server that cannot reach Hugging
  Face takes the folder from a machine that can: `--from-dir <copied folder>`.
  `--check` says whether GREMLIN will find and load it. GREMLIN picks it up on
  its next start.
* *Settings* (environment or `.env`): `GREMLIN_EMBEDDINGS=off` turns it off;
  `GREMLIN_EMBEDDING_MODEL_DIR` and `GREMLIN_EMBEDDING_CACHE_PATH` move the model
  and the cache. Without the packages or the model, the helper matches by words
  alone, as before, and the helper bar says why.

**Keeping it fast.** The index is built once per change to the work orders'
text or the code lists (`COUNT`, `MAX(mapped_record_id)` and `MAX(mapped_at)`,
answered from `idx_mapped_cmms_mapped_at`), and the decisions are re-read on
each change to GREMLIN.db, one narrow query. On a 43,000-work-order synthetic
plant, by words alone, the index builds in about 4 s, a suggestion takes about
12 ms, the calibration about 4 s, and the process holds about 120 MB.
With meaning matching on, the first reading of all 43,000 work orders took
about 5 minutes on a 4-core machine (in the background, with half the cores);
after that a restart reads the 46 MB vector cache in about 5 s, a suggestion
takes about 17 ms, the accuracy report (replayed twice, with and without
meaning) about 11 s, and the process peaks at about 450 MB.

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
   one, or name the part it replaced; its component is *not* implicitly renewed —
   the editor pre-fills the list with it unless a part is named. The form picks
   them from a component search, each shown as a removable chip.
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
12. **Induced failures** (cause semantic INDUCED) are counted as suspensions when
    *Count induced failures (installation and maintenance errors) as suspensions*
    is ticked: one whose work renewed the item ends its life as a suspension, one
    that renewed nothing ends no life, and the pattern views leave them out. The
    POP string records `induced=suspensions`, and the "Induced failures present"
    warning names the checkbox the same way.
13. **The Weibull reads sub-asset, component, part and mechanism only.** Failure mode,
    cause and action narrow Triage and the other views but not the life analysis,
    and the Weibull's selector does not offer them: one
    mechanism presents as several modes, so a mode filter turned same-mechanism
    failures into suspensions (acceptance case 168996); a cause beyond "induced"
    (its own checkbox, item 12) restates the mechanism; and the action is what was
    done after the failure, which the engine already reads to decide whether the
    item was renewed. A run with any of them ticked says so in an info warning, and
    is saved, named and keyed without them. Runs saved before this keep their
    selection and can still be opened, but no longer match a population picked on
    the page and are left out of the rankings.
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
    record class, sub-asset, the five codes, evidence, renewed items, qty,
    linked WO, note) as dropdowns where they come from a list. Its sheet is
    protected without a password: the header row is locked and columns can't be
    inserted or deleted, while every cell below, and rows added, stay free to
    edit, sort and filter. A hidden last column, `disposition_version`, holds the
    saved disposition each row was downloaded with.

    On upload the table is found on whichever sheet and under whichever top row
    holds it; headings are read in any capitals and under the page's names, a
    moved column where it went, and a heading two columns share refuses the
    workbook. Rows are matched by `mapped_record_id`, or by `task_id` when that is
    gone, and the two must agree when both are there. Codes are read in any
    capitals or by name. Only rows whose disposition columns differ from what is
    saved are saved, through the same checks as the form. A row that still reads
    as its downloaded version, where a newer one has been saved since, is left as
    GREMLIN has it; one changed both there and in the workbook is refused. Every
    refused row is listed at once, by sheet row and task, and one refuses the
    upload.
20. **Rankings** take only one-item populations (one component, or one part), so
    a beta is about one kind of item, and only the latest saved run of each population (none saved with a
    mode, cause or action, item 13).

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
* The disposition helper suggests a component but not yet a part; the part is
  left for the engineer.
