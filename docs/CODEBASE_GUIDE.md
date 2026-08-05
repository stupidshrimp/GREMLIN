# GREMLIN Codebase Guide

**A five-page introduction for developers who have never touched this repository.**

Read it front to back once. Afterwards, Page 5 is the one you will keep coming back to — it
explains how to actually make a change without breaking anything.

| Page | Title | What you get |
| --- | --- | --- |
| 1 | [What GREMLIN is, and how to run it](#page-1--what-gremlin-is-and-how-to-run-it) | The domain, the stack, the repo map, local setup |
| 2 | [The data pipeline: Limble → raw → mapped](#page-2--the-data-pipeline-limble--raw--mapped) | How maintenance data gets into the database |
| 3 | [The database and the analysis chain](#page-3--the-database-and-the-analysis-chain) | The tables, and the six stages one work order passes through |
| 4 | [The web layer: Flask, templates, and the browser client](#page-4--the-web-layer-flask-templates-and-the-browser-client) | Routes, the JSON API, Jinja, and the vanilla-JS front end |
| 5 | [The math, and how to make your first change](#page-5--the-math-and-how-to-make-your-first-change) | Weibull in plain English, plus four worked change recipes |

---

## Page 1 — What GREMLIN is, and how to run it

### The problem it solves

GREMLIN stands for **G**raphical **R**eliability **E**ngineering, **M**aintenance,
**L**ife-Data Analysis **IN**terface. It is an internal tool for reliability engineers at a
manufacturing plant.

The story it supports is this: a plant runs machines. Machines break. Every break, repair,
and scheduled service is logged as a *task* in a CMMS (Computerized Maintenance Management
System) called **Limble**. Buried in those thousands of tasks is the answer to questions
like *"is this pump wearing out, or does it fail randomly?"* and *"would replacing it on a
schedule actually help?"*.

GREMLIN pulls that raw maintenance history in, lets an engineer review and label each
record by hand, and then fits a **Weibull distribution** to the resulting failure history.
The output is two numbers — **beta** (the shape of the failure pattern) and **eta** (the
characteristic life) — plus charts and a Word report.

### Vocabulary you need before reading any code

| Term | Meaning in this codebase |
| --- | --- |
| **Asset** | One machine. Identified by an `asset_number` string like `"3101"`. Nearly every query filters on it. |
| **Work order (WO)** | A corrective repair task — something broke. |
| **PM** | Preventive maintenance — scheduled service, nothing broke. |
| **Failure mode / mechanism** | A two-level taxonomy of *how* something failed. Mode is broad ("Hydraulic leak"), mechanism is specific ("Seal degradation"). Modes contain mechanisms. |
| **Disposition** | A human decision about one record: does this count as a real failure for analysis, or not? This is the heart of the app. |
| **Censored observation** | A time interval where we know the machine ran *at least* this long without failing, but no failure ended it. Statistics still use it; throwing it away biases the answer. |
| **Beta (β)** | Weibull shape. `< 1` = infant mortality, `≈ 1` = random, `> 1` = wear-out. |
| **Eta (η)** | Weibull scale — the life by which ~63% of the population has failed. |

### The stack

Deliberately minimal. There is **no build step, no bundler, no ORM, and no front-end
framework.**

- **Backend:** Python 3.11 + Flask. Three dependencies total (`requirements.txt`):
  `Flask`, `requests`, `python-dotenv`.
- **Database:** SQLite, a single file, accessed with the standard-library `sqlite3` module
  and hand-written SQL.
- **Frontend:** Jinja2 templates, plain CSS, and vanilla JavaScript. Charts are drawn by
  hand on `<canvas>` with the 2D API — there is no Chart.js or D3.
- **File generation:** Excel `.xlsx` and Word `.docx` files are assembled from scratch
  using `zipfile` and raw OOXML strings (`_write_xlsx`, `_write_docx` in
  `services/life_data_service.py`). No `openpyxl`, no `python-docx`.

The only external resource the browser loads is the Inter font from Google Fonts
(`templates/base.html:9`).

### Repo map

```
app.py                      Flask app: every route + the JSON API (606 lines)
requirements.txt            Flask, requests, python-dotenv

services/
  life_data_service.py      ★ THE CORE. ~4,600 lines. Schema, mapping, disposition,
                              Weibull math, Excel + Word export.
  ingestion_service.py      Limble payload → the raw_json shape the mapper understands
  reliability_service.py    Placeholder / scaffolding (see below)
  classification_service.py Placeholder / scaffolding

repositories/
  raw_repo.py               ★ Writes immutable Limble payloads into the raw tables
  metrics_repo.py           Placeholder
  failure_repo.py           Placeholder
  analysis_repo.py          Placeholder

integrations/limble.py      Limble CMMS v2 HTTP client (auth, paging, retries)
jobs/sync_limble.py         CLI entry point: `python -m jobs.sync_limble`
models/dto.py               Two small dataclasses, barely used

templates/                  Jinja2 pages, all extending base.html
static/js/                  life_data_analysis.js (4,359 lines), metrics.js (880)
static/css/                 theme.css + one stylesheet per page area
tests/                      27 unittest tests

Reference/                  A frozen snapshot of the older PyQt6 desktop app. NOT wired
                            into the running web app. Read-only history — do not edit.
API/, Archive/, lib/        Experimental scripts and stubs. Not used at runtime.
```

**Important:** `services/reliability_service.py`, `services/classification_service.py`, and
three of the four repositories are **placeholder scaffolding** — every method returns an
empty dict, an empty list, or `None`, and each carries a `# TODO`. They are wired up in
`app.py:60` and feed the (also nearly-empty) Failure Classification page. Do not spend time
studying them expecting to find logic. All real behaviour lives in `life_data_service.py`
and `raw_repo.py`.

### Running it locally

The app needs a `GREMLIN.db` SQLite file. The hard-coded default is a Windows path,
`C:\GREMLIN\GREMLIN.db` (`services/life_data_service.py:36`), so on any other platform you
must override it:

```bash
python -m pip install -r requirements.txt
export GREMLIN_DB_PATH=/path/to/GREMLIN.db
python app.py                       # http://localhost:5000
```

If no override is set and the default file is missing, GREMLIN deliberately **refuses to
create an empty database** and raises a clear error instead (`app.py:100`). The reason is
subtle and worth knowing: on Linux, `C:\GREMLIN\GREMLIN.db` is just an ordinary relative
filename, so SQLite would happily create a junk file and the mistake would go unnoticed.

The `LifeDataService` is constructed **lazily on first use**, not at import
(`app.py:87`), so a bad database path produces a clean JSON error on the page instead of
crashing the whole server at boot.

### Running the tests

The `tests/` directory has no `__init__.py`, so `unittest discover` will not find it. Run
each module directly from the repo root:

```bash
PYTHONPATH=. python tests/test_downtime.py
PYTHONPATH=. python tests/test_raw_repo.py
PYTHONPATH=. python tests/test_negative_downtime_analysis.py
```

All 27 tests pass in about a second. They build a real SQLite database in a temp directory,
so they exercise the actual schema — no mocking.

---

## Page 2 — The data pipeline: Limble → raw → mapped

Data enters GREMLIN through a command-line sync job and passes through two storage layers
before any screen can see it. Understanding this pipeline explains most of the surprising
code in the repository.

```
Limble CMMS API
      │  integrations/limble.py      — HTTP, auth, pagination, retries
      ▼
IngestionService.transform()        — normalise one task into a raw_json dict
      │  services/ingestion_service.py
      ▼
raw_cmms_record.raw_json            — IMMUTABLE. The stored truth.
      │  repositories/raw_repo.py
      ▼
mapped_cmms_record                  — flat, typed, queryable columns
         services/life_data_service.py::refresh_mapped_cmms_records()
```

### Step 1 — The API client (`integrations/limble.py`)

`LimbleClient` does one job: talk HTTP and return **un-transformed** Limble payloads. It
handles Basic auth from a client id + secret, walks paginated list endpoints, sleeps ~1.1 s
between pages to respect a low rate limit, honours `Retry-After` on HTTP 429, and retries
5xx responses with exponential backoff.

Credentials come from the environment (`LIMBLE_CLIENT_ID` / `LIMBLE_CLIENT_SECRET`, with
older aliases accepted) via `LimbleConfig.from_env()`.

### Step 2 — Transform (`services/ingestion_service.py:195`)

`IngestionService.transform()` takes one Limble task and produces the `raw_json` dictionary
that everything downstream reads. It keeps the full API payload and *adds* fields:

- **`"Asset Number"`** is set from the numeric `assetID`. Every screen keys off
  `asset_number`, and the `/tasks` endpoint only carries a numeric id.
- **Dates** are emitted twice — the original Unix integers *and* ISO-8601 UTC strings under
  `*_Final` keys. The Weibull code parses the `*_Final` strings and cannot read Unix ints.
- **`downtime` is stored exactly as Limble sends it, in seconds.** This is deliberate:
  rescaling here would double-convert against the single normalisation point downstream.

`sync_all()` (line 59) orchestrates the run and also **drops Limble "template" tasks** —
those are PM *definitions*, not real events, and would contaminate the analysis.

### Step 3 — The raw layer (`repositories/raw_repo.py`)

Two tables: `import_batch` (one row per sync run) and `raw_cmms_record` (one row per task,
payload in `raw_json`).

Three behaviours here are non-obvious and each exists because of a real bug:

1. **Schema-aware inserts.** On a fresh database it creates a modern table shape, but on an
   existing production database it adapts to whatever columns are already there — including
   legacy `NOT NULL` columns the app no longer cares about, which get safe zero-values
   (`_insert_schema_aware`, line 350). A sync must never fail over a column nobody uses.

2. **Additive merge, not replace** (`_merge_preserved_fields`, line 436). A Limble
   `/tasks` refresh routinely returns a *narrower* payload than what is already stored —
   completion notes, completed dates, and downtime are often omitted. The old
   replace-the-row behaviour silently blanked exactly the fields the analysis depends on.
   Now the incoming payload is *overlaid* on the stored one, and an empty incoming value
   never overwrites a non-empty stored one. Read the docstring; it is the best explanation
   in the repo.

3. **The asset-number bridge** (`_build_asset_number_bridge`, line 503). Historical rows
   group work orders under a curated `Asset Number` like `"PUMP-01"`, but the API only
   supplies a numeric `assetID`. Without a bridge, new work orders land under a different
   asset and appear to vanish. The function builds a best-effort `assetID → curated number`
   lookup by voting across rows that already pair the two values.

Records are upserted on the Limble `taskID` with a SHA-256 content hash, so unchanged tasks
are skipped and `raw_record_id` stays stable — which keeps the foreign keys from
`mapped_cmms_record` valid.

### Step 4 — The mapping layer (`services/life_data_service.py:940`)

`refresh_mapped_cmms_records()` walks every raw row, hashes the JSON, and re-derives
`mapped_cmms_record` for any row whose hash *or mapper version* changed. `_map_raw_record`
(line 1019) is the pure function that flattens one JSON blob into ~45 typed columns.

Three things worth internalising:

- **`raw_json` is never modified.** Everything in `mapped_cmms_record` is derived and can
  be thrown away and rebuilt. This is the single most important invariant in the codebase.
- **`_MAPPING_VERSION`** (line 127) is a version stamp on every mapped row. Bump it whenever
  `_map_raw_record`'s output changes, and already-mapped rows get re-derived automatically
  on the next service construction. That is how the "downtime in seconds" fix reached
  production databases without a manual migration step.
- **`record_class_final` is preserved on update.** It holds a human decision; a remap must
  never overwrite it (see the `ON CONFLICT` clause at line 997).

### Auto-classification (`_classify_record`, line 1147)

Each mapped row gets a *guess* at what kind of record it is — `CORRECTIVE_WO`, `PM`,
`INSPECTION`, `PARTS_ORDER`, `PROJECT_WORK`, or `UNKNOWN` — from keyword matching over the
task name, request title, description, and completion notes ("leaking", "broken", "repair"
→ corrective; `" - M - "`, `" - Q - "` frequency codes → PM).

This is a heuristic, and it is meant to be. It only produces `record_class_auto`, a
starting point. A human overrides it via `record_class_final` on the Disposition screen.

### The downtime rule (`_parse_downtime_minutes`, line 1122)

One function, one rule: **a bare number is seconds.** `12600` → 210 minutes → 3.5 hours.
Text with explicit units is still honoured (`"3.5 hours"`, `"45 min"`) for legacy
hand-entered values.

Getting this wrong inflated every downtime figure by 60×. `tests/test_downtime.py` is the
regression guard, and the paired `downtime_source_value` / `downtime_source_unit`
provenance fields exist purely to protect rows imported by a retired code path from being
re-scaled a second time.

---

## Page 3 — The database and the analysis chain

Everything lives in one SQLite file. `LifeDataService.ensure_schema()`
(`services/life_data_service.py:309`) creates ~20 tables with `CREATE TABLE IF NOT EXISTS`
and runs on **every** service construction, so it is idempotent by design.

### One work order's journey, in six stages

This is the mental model that makes the schema make sense. A single record moves left to
right, and each stage is a different table:

```
① raw_cmms_record        the untouched Limble JSON
        ↓  _map_raw_record
② mapped_cmms_record     flat columns + an auto-guessed record class
        ↓  a human clicks "Save" on the Disposition screen
③ event_disposition      "this IS a real hydraulic-seal failure" (or: exclude it)
        ↓  _refresh_event_processing
④ event_processing_record ordered timeline of failures + PM resets for one population
        ↓  _refresh_observations
⑤ weibull_observation    the GAPS between events = life intervals, in hours
        ↓  _fit_weibull_2p
⑥ weibull_result         beta, eta, confidence intervals, MTTF, B10/B50
```

Stages ④–⑥ are **fully derived and disposable**. Every analysis run deletes and rebuilds
them for that population (`_delete_population_weibull_artifacts`, line 3620). Only stages
①–③ hold information that cannot be recomputed.

### The table groups

**Import + mapping:** `import_batch`, `raw_cmms_record`, `mapped_cmms_record`.

**Taxonomy:** `failure_mode`, `failure_mechanism` (a mechanism optionally belongs to a
mode), plus `asset_failure_mode_option` and `asset_failure_mechanism_option`, which track
which labels have been used on which asset so the dropdowns can offer relevant choices
first.

**Human decisions:** `event_disposition` — the most important table in the app.

**Grouping:** `modeled_population` — one row per (asset, failure mode, optional mechanism)
combination. A Weibull analysis is always run against exactly one population.

**Life calculation:** `life_basis`, `asset_schedule_class`, `schedule_exception`,
`event_processing_record`, `weibull_observation`.

**Results:** `analysis_dataset`, `analysis_dataset_member`, `weibull_analysis_run`,
`kaplan_meier_point`, `weibull_result`, `weibull_curve_point`,
`weibull_parameter_adjustment`, `approved_weibull_parameter`, `weibull_report_log`.

### The `is_current` pattern

Dispositions are **never updated in place**. Saving a new one flips the old row's
`is_current` to 0 and inserts a fresh row (`_save_disposition_with_conn`, line 3891). A
partial unique index enforces one current row per record:

```sql
CREATE UNIQUE INDEX ux_event_disposition_one_current
  ON event_disposition(mapped_record_id) WHERE is_current = 1;
```

You get a free audit trail, and every read query carries `AND d.is_current = 1`. The same
pattern is used for `weibull_parameter_adjustment` and `approved_weibull_parameter`.

### Disposition validation

`_save_disposition_with_conn` (line 3771) is ~140 lines of business rules before a single
write happens. A sample:

- A PM record can never be saved as `INCLUDED_FAILURE`.
- `HELD_AMBIGUOUS` and `EXCLUDED_MIXED_CONTAMINATING` require written notes.
- `INCLUDED_PM_RESET_EVENT` requires an `APPROVED_RESET` decision, a reset-target failure
  mode, *and* a written rationale.
- A PM reset target must already be a failure mode that was dispositioned on this asset
  from the WO screen — you cannot invent taxonomy from the PM screen.
- A chosen mechanism must belong to the chosen mode.

These are not arbitrary. Each one prevents a class of statistically invalid analysis. When
you touch this function, assume every rule is load-bearing.

Two derived flags come out the other side: `include_in_event_processing` and
`include_in_weibull_candidate`. Stage ④ reads *only* rows where both are 1.

### Writing to SQLite safely

The database lives on a shared network drive with multiple concurrent users. That drives
three unusual choices in `write_connection()` (line 226):

- **`BEGIN IMMEDIATE`** reserves the single writer slot up front rather than discovering a
  conflict mid-transaction.
- **`PRAGMA journal_mode = DELETE`** (rollback journal, *not* WAL) — WAL is unsafe on many
  network filesystems.
- **A 30-second busy timeout**, after which `_database_write_error` (line 281) translates
  the raw SQLite error into plain English: *"another GREMLIN user is writing… wait a moment
  and try again"*, complete with the database path and a suggested action.

**Rule: every write must go through `with self.write_connection() as conn:`.** Reads use
`self.connect()`, which returns a `ClosingSqliteConnection` that closes itself on context
exit (line 54) — the stock `sqlite3` context manager commits but leaves the handle open,
which was leaking handles during large Excel imports.

### Schema migration

There is no Alembic and no migration files. `_migrate_rel_disposition_schema` (line 784)
runs before the `CREATE TABLE` block and does the job by hand: for each expected column, if
`PRAGMA table_info` says it is missing, `ALTER TABLE … ADD COLUMN`. Adding a nullable column
is therefore a two-line change. Anything harder — renames, type changes, dropped columns —
has no supported path, and you should ask before attempting it against a production
database.

---

## Page 4 — The web layer: Flask, templates, and the browser client

### `app.py` in one pass

The whole web layer is a single 606-line module with no blueprints. Its shape:

1. **`ICONS` + `PAGES`** (line 26–55) — inline SVG strings and the page registry. `PAGES`
   drives `NAV_LINKS` (line 166), which every template receives to render the sidebar. Add
   a page here and navigation updates itself.
2. **Service wiring** (line 60) — the placeholder `ReliabilityService`, plus the lazy
   `get_life_data_service()`.
3. **Page routes** — each one is 3–6 lines: `render_template(..., nav_links=NAV_LINKS)`.
   Almost no logic. Data arrives later over JSON.
4. **The JSON API** — the real surface area.

### Error handling: the `@life_data_api` decorator

Every JSON endpoint is wrapped by `life_data_api` (line 122), which turns exceptions into
consistent payloads so the client only ever has to read `data.error`:

| Exception | Status | Meaning |
| --- | --- | --- |
| `LifeDataApiError` | its own (400/403/503) | Deliberate, user-facing message |
| `DatabaseWriteError` | 503 | SQLite locked / unwritable |
| `ValueError` | 400 | A service-layer validation rule rejected the input |
| anything else | 500 | `"Unexpected error: …"` |

`_service_or_api_error()` (line 141) wraps a failed database open into a 503 that names the
exact path that was tried — so a misconfigured `GREMLIN_DB_PATH` shows a helpful banner
rather than a stack trace.

### The endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` `/metrics` `/settings` `/standards-and-documentation` | Static pages |
| GET | `/life-data-analysis` | Workflow landing page |
| GET | `/life-data-analysis/perform-analysis` | The main analysis workspace |
| GET | `/life-data-analysis/disposition` | The disposition editor |
| GET | `/life-data-analysis/failure-classification` | Mostly a placeholder |
| GET | `/life-data-analysis/api/assets` | Asset dropdown options |
| POST | `/life-data-analysis/api/refresh-mapping` | Re-run the raw → mapped map |
| GET | `/life-data-analysis/api/summary` | Readiness counts + Pareto + beta rankings + trend |
| GET | `/life-data-analysis/api/dispositions` | Paged disposition rows (50/page) + dropdown options |
| POST | `/life-data-analysis/api/dispositions/save` | Save a batch of dispositions |
| GET/POST | `/life-data-analysis/api/dispositions/excel` | Download / upload the Excel round-trip |
| GET | `/life-data-analysis/api/weibull-groups` | Populations ready to analyse |
| POST | `/life-data-analysis/api/perform-analysis` | **Run the Weibull fit** |
| POST | `/life-data-analysis/api/calculate-all` | Fit every group (password-gated) |
| POST | `/life-data-analysis/api/parameter-adjustment` | Save a manual beta/eta override |
| POST | `/life-data-analysis/api/weibull-report` | Generate the `.docx` report |
| GET | `/life-data-analysis/api/pm-effectiveness` | PM effectiveness analysis |
| GET | `/life-data-analysis/api/downtime-drivers` | Downtime driver analysis |
| GET | `/metrics/api/reliability` | Per-asset KPIs for the Metrics dashboard |

**Note on `calculate-all`:** it is gated by `MLE_CALCULATION_PASSWORD = "1336"`, a constant
at `app.py:71`. It is a "are you sure?" speed bump against an expensive operation on an
internal tool, not a security control. Treat it as such.

### File downloads

Excel and Word downloads follow one pattern (`api_download_disposition_excel`, line 384):
build into a `tempfile`, read the bytes into memory, delete the temp file in a `finally`
block, then `send_file` from a `BytesIO`. Repeated downloads never orphan files on disk.

### Templates

Classic Jinja inheritance. `base.html` defines the shell and three blocks —
`head_extra`, `content`, `scripts` — and every page starts with
`{% extends "base.html" %}`. `topbar.html` and `sidebar.html` are `{% include %}`d; the
sidebar loops over `nav_links` and marks the active item by comparing `request.path`.

Templates are **structure only**. They contain empty `<div>`s with ids and hidden panels;
JavaScript fills everything in after an API call. `settings.html` is the one exception with
a small inline `<script>`.

### The browser client

Both `life_data_analysis.js` and `metrics.js` follow the identical shape:

```js
(function () {
  "use strict";
  const API = "/life-data-analysis/api";
  const state = { /* every piece of page state, in one object */ };
  const $ = (id) => document.getElementById(id);
  function el(tag, attrs, children) { /* tiny createElement helper */ }
  async function requestJson(url, options) { /* fetch + unwrap data.error */ }
  // ... feature sections, separated by `// ---- name ---` banner comments
  function init() { /* attach listeners, load data */ }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
```

Conventions worth copying when you add code:

- **One `state` object.** No framework, no reactivity — you mutate `state` and then call a
  `render*()` function explicitly.
- **Monotonic tokens against stale responses.** `state.dispositionToken`, `pmToken`,
  `downtimeToken`, `summaryToken`: increment before a fetch, and on resolve only render if
  the token still matches. Debounced searches resolving out of order used to leave the
  table filtered for a previous query.
- **`el()` everywhere, not string HTML.** DOM is built node by node; `innerHTML` is
  reserved for trusted icon markup.
- **Charts are hand-drawn** on `canvas.getContext("2d")` and redrawn on `window.resize`
  (`state.analysisRedraw` holds the redraw closure for the current result).
- **Section banner comments** (`// ---- disposition editor ----`) are the navigation aid in
  a 4,300-line file. Use them; `grep` for them.

`life_data_analysis.js` serves **two** pages. `init()` branches on the presence of
`#lda-disposition-root` to pick `initDispositionPage()` or `initAnalysisPage()`
(line 4232), so the asset-picker combobox is written once and shared.

> **Gotcha:** `static/js/layout.js` (a sidebar collapse toggle) is not referenced by any
> template and no `#sidebarToggle` element exists. It is currently dead code. Don't be
> confused when your changes there have no effect.

---

## Page 5 — The math, and how to make your first change

### Weibull analysis in plain English

Take one machine and one failure mechanism. Line up every confirmed failure in date order.
The **gaps between consecutive failures** are how long the machine survived each time. Feed
those survival times to a Weibull fit and you get:

- **beta (β)** — the shape. Below 1, failures are *decreasing* over time (bad installs,
  infant mortality). Around 1, failure is *random* (age-based replacement is useless).
  Above 1, failures *increase* with age — genuine wear-out, and a scheduled replacement
  interval can be justified.
- **eta (η)** — the scale, in hours. Roughly the life by which 63% of the population has
  failed.

**Censoring** is the subtle part. The last interval, from the most recent failure to today,
hasn't ended in a failure yet. Discarding it would bias the estimate pessimistically, so it
is recorded as a *right-censored* observation: "survived at least this long". Approved PM
resets create censored intervals too — the clock restarts without a failure.

### `perform_weibull_analysis()` step by step (line 3914)

```
1. Validate the grouping level (FAILURE_MODE or FAILURE_MECHANISM)
2. Set the analysis cutoff = now (UTC)
3. Open ONE write transaction for the entire run
4. _get_or_create_modeled_population()   → the population id
5. _refresh_event_processing()           → stage ④: ordered failure/PM-reset timeline
6. _refresh_observations()               → stage ⑤: gaps → schedule-adjusted life hours
7. _fit_weibull_2p()                     → beta, eta, log-likelihood
8. _weibull_confidence_intervals()       → 95% CIs
9. _kaplan_meier_points() + _curve_points()  → the plot data
10. INSERT analysis_dataset → run → km points → curve points → weibull_result
11. Return an AnalysisResultView dataclass, which app.py serialises to JSON
```

**Schedule-adjusted hours** (`_scheduled_life_hours`, line 4102): elapsed calendar time is
not run time. A machine on a 20 h/day weekday schedule that sits idle all weekend did not
age 48 hours over Saturday and Sunday. The function walks the interval day by day,
excluding weekends and prorating each weekday by `hours_per_day / 24`. A hard-coded set of
asset numbers runs 24 h weekdays instead (`WEEKDAY_24H_ASSET_NUMBERS`, line 28).

**The fit itself** (`_fit_weibull_2p`, line 4350) is pure standard-library math — no SciPy.
The MLE score equation for beta is bracketed by scanning 400 points across `[0.1, 20]` and
then bisected 80 times; eta follows in closed form. Confidence intervals come from a
finite-difference Hessian of the log-likelihood evaluated on `log(beta), log(eta)`, so the
interval endpoints stay positive after exponentiation (line 4398).

**The engineering advice** returned alongside the numbers (`_beta_recommendation` and
friends, lines 4475–4504) is prose written by a reliability engineer, keyed off thresholds
at β < 0.9, β ≤ 1.1, and above. If someone asks to "change the recommendation", that is
a text edit in these methods — not a math change.

---

### Recipe 1 — Add a new page

1. Add an entry to `PAGES` in `app.py:34` (route, template, title, icon). Reuse an existing
   `ICONS` key or add a new inline SVG.
2. Add a route function returning
   `render_template("your_page.html", page_title="…", nav_links=NAV_LINKS)`.
3. Create `templates/your_page.html` starting with `{% extends "base.html" %}` and a
   `{% block content %}`.
4. Need page-specific CSS or JS? Add them in the `head_extra` and `scripts` blocks, as
   `perform_analysis.html` does.

The sidebar picks the page up automatically from `NAV_LINKS` — unless you exclude it, the
way Standards and Documentation is excluded at `app.py:169`.

### Recipe 2 — Add a JSON endpoint

```python
@app.route("/life-data-analysis/api/your-thing")
@life_data_api                       # ← never omit this
def api_your_thing():
    service = _service_or_api_error()   # ← never call get_life_data_service() directly
    asset_number = _required_asset()    # ← reuse the validators
    return jsonify({"your_thing": service.your_thing(asset_number)})
```

Then add the method to `LifeDataService`, near related code, using `self.connect()` for
reads. Raise `ValueError` for bad input — the decorator turns it into a clean 400.

On the client, call it with `getJson()`/`postJson()`, guard against stale responses with a
token if it can be triggered repeatedly, and render into an existing hidden panel.

### Recipe 3 — Change how records are classified

Edit `_classify_record` (`services/life_data_service.py:1147`) — the keyword lists live
right there. Then **bump `_MAPPING_VERSION`** at line 127 (`"v2"` → `"v3"`). Without the
bump, existing databases keep their stale `record_class_auto` values, because
`refresh_mapped_cmms_records` skips rows whose content hash *and* mapper version both
match.

Note that `record_class_final` — the human override — is untouched by a remap, which is
exactly what you want.

### Recipe 4 — Add a database column

1. Add it to the `CREATE TABLE` block in `ensure_schema()` (line 309), for fresh databases.
2. Add it to the matching dict in `_migrate_rel_disposition_schema()` (line 784), for
   existing databases: `{"your_column": "TEXT"}`. Nullable, or with a `DEFAULT`.
3. If it derives from raw JSON, populate it in `_map_raw_record` and bump
   `_MAPPING_VERSION`.
4. If a screen shows it, add it to `DISPLAY_COLUMNS` (line 129) and to the relevant
   `SELECT` in `disposition_rows` (line 2557).

Both steps 1 and 2 are required — one covers new installs, the other covers production.

---

### Conventions and gotchas — the short list

- **Never mutate `raw_cmms_record.raw_json`.** Everything else is derived and rebuildable.
- **All writes go through `write_connection()`.** All reads through `connect()`.
- **Bump `_MAPPING_VERSION` whenever `_map_raw_record` output changes.**
- **`is_current = 1` belongs in every disposition read query.**
- **Downtime: a bare number is seconds.** One conversion point, in
  `_parse_downtime_minutes`.
- **`asset_number` is a string**, not an int (`"3101"`). Sorting uses `_natural_key`
  (line 1218) so `"10"` sorts after `"9"`.
- **Dates are ISO-8601 strings in SQLite.** Parsing is centralised in `_parse_datetime`
  (line 4329), which accepts six formats because historical data is messy.
- **Prefer `COALESCE(record_class_final, record_class_auto)`** — the human decision wins
  over the guess. This idiom appears in almost every analytical query.
- **No `pip install` for new libraries** without discussing it. The zero-dependency Excel
  and Word writers exist because deployment is locked down; adding `openpyxl` would defeat
  that on purpose-built code.
- **`Reference/` is a frozen snapshot** of the older PyQt6 desktop application, kept for
  historical comparison. It is not imported at runtime. Editing it does nothing.

### Where to start reading

If you have 30 minutes, read these in order:

1. `app.py` end to end — it is the map of the whole application.
2. `services/ingestion_service.py` — small, well-commented, and it teaches the data shape.
3. `services/life_data_service.py:309–760` — the schema. Everything else is queries against
   it.
4. `services/life_data_service.py:3914–4070` — `perform_weibull_analysis`, the payoff.

Then pick a small bug and use Recipe 2. The comments in this codebase are unusually good —
most of them explain *why*, and several document bugs that were expensive to find. Read
them before changing the line above them.
