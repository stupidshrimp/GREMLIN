# Availability Dashboard — Design Plan

Design for the Availability card on the Metrics page, replacing the "Coming soon"
placeholder in `templates/metrics.html`. The target is the
`Availability_Dashboard_V6TEST.xlsm` workbook: nine per-asset-group charts, each
showing monthly availability as bars per asset plus an Average line and a Goal
line.

## 1. Scope

Replicate the workbook's nine charts on the Metrics page:

| Asset group | Assets | Net h/day |
|---|---|---|
| Salvagnini | 3101–3107 | 18 |
| Building 12 Cloos Robots | 2743–2746 | 16 |
| Building 6 Finishing | 4001, 4002 | 16 |
| Building 9 Plating Lines | 1935, 1934, 4000 | 16 |
| Building 6 LVDs and Press Brakes | 3147, 3150, 2499, 3028, 2689 | 16 |
| Building 5 Mazak Lasers | 3000, 2728 | 16 |
| Building 1 Secondary Finishing | 505, 1682, 4028, 758, 3326, 2667, 987 | 16 |
| PPD Hedrich Dispensers | 3154, 3142, 3023, 3253 | 18 |
| PPD Sandblasters | 3359, 3461, 3325, 3160, 2958, 3073 | 16 |

`Dilo & Enervac` exists in the workbook with `Include? = N` and no asset list, so
it is configured but not charted — nine charts, not ten.

## 2. Decisions

### 2.1 All downtime counts, regardless of work-order type

Availability sums **every** work order with `downtime > 0`. No filter on
`type_raw`, `record_class_final/auto`, or `is_pm_candidate`.

This deliberately differs from both prior implementations, and the reasons matter:

- **The workbook excludes `type = 4`** with the comment *"Type 4 rows are PMs in
  the current Limble extract."* That comment is wrong. Cross-tabulating all
  43,320 raw rows shows type 1 is PM (15,823 rows, names like
  `DOCK LEVELLER PM INSPECTION`), type 6 is work requests (20,305), type 7 is
  parts alerts (4,170), type 4 is projects and misc repairs (2,410), type 2 is
  request templates (571).
- **`Reference/availability_dashboard/availability_calculator.py` excludes
  `is_pm_candidate`**, which `services/life_data_service.py:1152` sets via
  `re.search(r"\bpm\b", ...)` across name + description + completion notes. At
  this plant, technicians close downtime work orders with return-to-service
  times — `"RTS AT 4:30 PM on 01/08/26"` — so the regex matches the *clock*
  "pm". 102 of 108 false positives came from `completionNotes`. Adopting that
  rule would have silently dropped **623.7 h, 16.3% of 2026 downtime**, and
  preferentially dropped the best-documented breakdowns.

Counting everything also decouples availability from the text classifier
entirely, so re-classifying a record on the disposition screen can never
retroactively move availability history.

Measured effect versus the workbook: **12 asset-months change, +71.2 h total.**
The only visible one is asset 3107 in March 2026 (+25.0 h, −6.3 points);
everything else is under 1.5 points. Expect that dip — it is not a porting bug.

PMs currently carry 0.0 downtime hours across 842 rows, so including them
changes nothing today. It only matters if Limble starts recording PM downtime,
which is the intended behaviour.

### 2.2 The card owns its own month window

The Availability card **ignores the Metrics page's shared date-range filter** and
carries its own month-window control. Availability is only defined over whole
calendar months; a `Mar 15 – Apr 20` range has no meaning.

The current (partial) month is excluded — a partial month always reads low. The
workbook includes it, which is a bug worth not copying.

**The default window is the 5 most recent complete months.** On 2026-08-06 that
is Mar–Jul 2026. The window is then clamped to the months that actually have
work-order data, so a database with three months of history shows three months
rather than two empty columns.

The workbook's rolling-window routine is named `UpdateRollingFiveMonthWindow`
but sets `MAX_MONTHS = 12`; five appears to have been the original intent, and
the workbook currently renders eight columns because that is how much 2026 data
exists. Neither number is load-bearing — the user picks the window, and five is
only where it starts.

### 2.3 The card ignores the asset filter

The card renders all nine groups and every asset in them, exactly like the
workbook. It does not respond to the page's asset multi-select.

This is required for correctness, not just fidelity: the Average line is a
group-level statistic, and linked downtime (§2.4) makes an asset's number depend
on assets that a filter might have hidden. Filtering to asset 3102 alone would
show a value driven by four machines not on screen.

**Consequence:** the card sits below two cards that *do* respond to the filter
bar, and it counts all downtime while the KPI card above counts corrective-only.
The card header must state its own scope ("All asset groups · all downtime ·
monthly"), and the filter bar must note that Availability is excluded.

### 2.4 Linked downtime is one-level and non-recursive

Salvagnini assets are mechanically coupled. A parent's adjusted downtime is its
own downtime plus a share of each linked asset's **direct** downtime:

```
adjusted_downtime(parent, month) =
    direct_downtime(parent, month)
  + Σ over rules  direct_downtime(linked, month) × impact_factor
```

Because the sum reads *direct* (pre-link) downtime, it never cascades. Verified
against the workbook for January 2026, asset 3102, rules → 3107, 3101, 3105,
3106 at 0.5 each:

```
(6.70 + 10.00 + 29.17 + 3.00) × 0.5 = 24.43 h linked
12.50 direct + 24.43 linked = 36.93 h adjusted → 90.6734 %
```

Three properties are intentional but non-obvious, and should be understood
before anyone questions the numbers:

- **No overlap merging.** Two linked assets down simultaneously are counted
  twice.
- **No cap.** Adjusted downtime can exceed scheduled hours; the result is
  clamped to 0% at display and marked by the `Flagged` indicator.
- **Asymmetric.** Asset 3101 (MV) is a pure source: it has no parent rules, so
  its own availability never reflects anything else, yet its downtime is charged
  at 50% to five other assets.

All 13 seeded rules are Salvagnini at 0.5. Other groups compute direct-only.

**Rules outlive membership.** A rule can name an asset that is no longer in any
included group — after it is removed from a group, or after its group is
excluded. Work orders are therefore loaded for group members *plus* every asset
named by a live rule, while only group members are charted. Loading just the
members would make the rule contribute zero, which reads as an improvement in
the parent's availability rather than as missing data. The month window still
follows the charted assets, so a decommissioned asset's history cannot stretch
the axis.

### 2.5 Nothing derived is stored — every request recalculates

Availability results are a pure function of (config + work orders) and are
**computed on request, never persisted**. The relevant slice is ~2,000 rows out
of 43,320; the cost is milliseconds.

This drops `availability_results` from the previous attempt's schema. Persisting
results recreates the workbook's core failure mode: numbers are only correct if
someone remembered to run `Update_Charts`. There is no recalculate button and no
staleness.

Config changes therefore apply to **all** months, including already-reported
ones. This is a deliberate choice over effective-dated schedule rows: one
schedule applied uniformly keeps every month computed on the same basis, so
month-over-month comparison stays apples-to-apples. Effective dating would make
January-at-18h and August-at-14h incomparable even though both are individually
"correct."

Mitigations, in place of versioning:

- `updated_at` on every config table.
- The chart states its basis — `Computed 2026-08-06 · Salvagnini 18.0 net h/day`
  — so assumptions travel with any screenshot or export.
- Saving a schedule change confirms first. The message must **not** cite the
  length of the currently displayed window — a schedule change applies to every
  month that has data, including months scrolled out of view and months already
  reported. Word it as scope, not count: *"This changes availability for every
  month, including ones already reported."*

### 2.6 Config lives in SQLite, seeded from code

User-editable config persists in `GREMLIN.db`. It is shared across users on the
network drive, already the app's write target with a tested failure path
(`write_connection`, `BEGIN IMMEDIATE`, the "Database write failed" popup), and
one backup covers everything.

Rejected: a JSON/YAML file (the app runs from a git checkout via
`start_gremlin.bat`, so a repo file is clobbered on update; a file on the share
needs its own locking and loses atomicity), and code-only constants (any goal
change would need a developer, defeating the point).

Defaults seed from `availability_config.py` with `INSERT OR IGNORE`, so a fresh
database works with zero setup while user edits persist.

### 2.7 One source of truth for group membership

Group membership currently exists in four places:
`static/js/metrics.js:29` (`DEFAULT_ASSET_NUMBERS`),
`Reference/availability_dashboard/availability_config.py:14`, the workbook's
`Configurator!B9:B17`, and the workbook's hidden `Asset Map` sheet.

The workbook already shows the failure mode: `EnsureRequiredAssetRowsInAvailabilityData`
is a VBA function that exists solely to patch asset 987 (Pangborn) into Building 1
Secondary Finishing after it was missed in one of the lists.

`availability_asset_group_assets` becomes the single source. `metrics.js` fetches
the asset list from the API and its hardcoded array is deleted; that array now
only seeds the KPI/Alerts default selection, which can flatten the API response.

### 2.8 Charts are hand-drawn on canvas

No charting library. The app ships zero JS dependencies today and
`static/js/metrics.js` already hand-draws every chart; the availability chart is
a fixed shape (N bars per month + 2 line overlays). Adding a CDN dependency to a
plant-floor app that may not have outbound internet is a risk the library does
not repay here.

## 3. Calculation

For each included group, each asset in it, each month in the window:

```
scheduled_hours       = weekday_count(month) × net_scheduled_hours_per_day
net_scheduled_h_day   = max(0, schedule_h − (break_h + lunch_h + setup_h))
direct_downtime_h     = Σ downtime for work orders where
                          asset matches
                          and downtime > 0
                          and created_date (local) falls in the month
linked_downtime_h     = Σ direct_downtime(linked, month) × impact_factor
adjusted_scheduled_h  = scheduled_hours + manual_ot_hours
adjusted_downtime_h   = direct_downtime_h + linked_downtime_h
availability          = max(0, (adjusted_scheduled_h − adjusted_downtime_h)
                                / adjusted_scheduled_h)
```

Group `Average` is the unweighted mean of its assets' availability for that
month, matching the workbook's `=AVERAGE(...)`. `Goal` defaults to 0.95.

Notes:

- **Downtime is bucketed by created date**, not completion date. A work order's
  downtime can therefore land in a different month than the work. The workbook
  documents this on `Configurator!A23:A24` and counts month-crossing work orders
  in an `Overlap` column, which is carried over.
- **Weekdays only.** Scheduled hours count Mon–Fri regardless of the group's
  hours/day, which is why Salvagnini's 24 h/day operation gets 22 × 18 = 396 h in
  January. See §6.
- **Negative downtime is skipped**, matching both prior implementations.
- **No work orders means 100%.** An asset nobody logs work against is
  indistinguishable from a perfect one, so the workbook's `Total WO Count`,
  `Zero Downtime WO Count`, `No WO Entries Flag` and note columns are carried
  over and surfaced on the card. When *nothing at all* can be computed, the card
  shows an empty state instead of nine charts of flat 100% — "no downtime
  recorded" and "no data" are the same arithmetic and very different facts.
  Three situations produce that empty card and each names the thing to go fix:
  no asset groups configured, work orders present but none for the configured
  assets (a configuration gap, not a missing import), or no complete month yet.
  Once *any* charted asset has data the window resolves normally, and the quiet
  assets render at 100% with their no-entry note.
- **Availability is `null`, not 0% or 100%, when there are no scheduled hours.**
  Only reachable by configuring a group down to zero net hours, but either
  substitute renders as a real number: 0% reads as a total outage and 100% as a
  perfect month. The Average line skips undefined months rather than dragging
  toward zero.

## 4. Data model

Seven config tables. All already exist in production databases from the earlier
attempt, but **with a different schema**, so they are migrated on bootstrap
rather than merely created — see §4.1.

| Table | Holds |
|---|---|
| `availability_asset_groups` | schedule / break / lunch / setup → net h/day, include flag, sort |
| `availability_asset_group_assets` | group membership (§2.7) |
| `availability_asset_display_names` | MV, PA, L3, ACN … |
| `availability_linked_downtime_rules` | parent, linked, impact factor |
| `availability_goal_percent` | per group, per month |
| `availability_manual_ot` | per asset, per month |
| `availability_settings` | timezone; `selected_year` dropped (now UI state) |

Changes from the earlier attempt's schema:

- **Drop `availability_results`** (§2.5). It is left in place in existing
  databases rather than dropped — that would destroy data someone may want — and
  `_KNOWN_ORPHAN_PREFIXES` in `services/schema_service.py:172` narrows from the
  `availability_` *prefix* to naming `availability_results` specifically, so the
  developer dashboard stops flagging the six live config tables as belonging to a
  removed feature.
- **Add `updated_at`** to each config table (§2.5). No `updated_by`: the app has
  no login, and a fabricated attribution is worse than none.
- **`availability_settings.utc_offset_hours` → a timezone name.** A flat 5-hour
  offset is applied year-round today; if the plant is on US Central that is
  correct only during DST, so January is off by an hour. This only affects work
  orders created within an hour of a month boundary, but `zoneinfo` costs nothing.

`availability_goal_percent` and `availability_manual_ot` are keyed by month, so
editing one month's value cannot disturb another. Only the schedule, membership
and link rules are global across time, per §2.5.

### 4.1 Migrating the earlier attempt's tables

An earlier draft of this plan claimed `CREATE TABLE IF NOT EXISTS` plus
`INSERT OR IGNORE` was enough to reuse the existing tables. That is true of
their *data* and false of their *schema*, and the schema changed in three ways:

| Table | Legacy shape | Now |
|---|---|---|
| `availability_settings` | `selected_year NOT NULL`, `utc_offset_hours`, `last_updated` | `timezone`, `updated_at` |
| `availability_asset_groups` | `net_scheduled_hours_per_day NOT NULL` | derived, not stored; `updated_at` added |
| `availability_manual_ot` | keyed `(asset_group, asset_number, month_date)` | keyed `(asset_number, month_date)` |

Everything else gained `updated_at`, and `availability_linked_downtime_rules`
gained a `UNIQUE(parent, linked)` constraint it did not have.

Left unmigrated, the first availability request on an existing installation
fails with `no column named timezone` — which is the *expected* state of every
real database, not an edge case.

So `ensure_schema` compares each table's columns against the target and rebuilds
any that differ: create the new shape, copy the columns the two have in common,
swap it in. That also repairs the missing unique constraint, which
`ALTER TABLE ADD COLUMN` cannot. Columns that existed only in the old shape are
dropped — they have no meaning under the current model. Rows that collide on a
newly-narrowed key (an asset whose overtime was recorded under two groups) keep
the later edit.

The rebuild runs with foreign keys disabled on its own connection, because
SQLite only honours a `foreign_keys` change outside a transaction, and
`availability_asset_group_assets` references the group table being rebuilt.
Group ids are copied, so those references survive.

`availability_results` is not migrated and not dropped — see §2.5.

## 5. Interfaces

```
GET  /metrics/api/availability                  → groups, months, series, goals, flags, basis
GET  /metrics/api/availability/config           → schedule, membership, names, rules
PUT  /metrics/api/availability/config/group/<g> → schedule edit
PUT  /metrics/api/availability/goal             → {group, month, percent}
PUT  /metrics/api/availability/ot               → {asset, month, hours}
```

Editing is split by how often a value changes and how much it moves:

- **Inline on the card** — Goal % and Manual OT hours. These are edited while
  looking at the chart ("March's goal should be 97%"), are per-month keyed, and
  carry no history risk.
- **Settings page** — schedule hours, group membership, display names, linked
  rules. Rare, higher blast radius, deserves a deliberate context switch and a
  confirm step. `templates/settings.html` has an empty "System Configuration"
  tile and a working fetch/banner pattern in the CMMS-refresh card to copy.

  Display names sit inside each group's panel, next to the membership that
  decides which bars exist, and save per asset on blur. They are the one
  Settings edit that does *not* take the recompute warning: a chart label is
  cosmetic and carries none of the schedule's blast radius.

Schedule editor rules:

- Net hours is **derived and read-only** — the workbook has it as a formula
  (`Configurator!G9 = MAX(0, C9-SUM(D9:F9))`), so a typed net that disagrees with
  its parts must be impossible.
- Validate: base hours 0–24, net ≥ 0, impact factor 0–1, goal 0–100%, OT ≥ 0.
- Offer "reset to defaults" per group, since the defaults are the known-good
  workbook values.

Code placement follows the existing layout: the calculator becomes
`services/availability_service.py`, the repository
`repositories/availability_repo.py`. The PyQt files
(`availability_charts.py`, `availability_widgets.py`) are not ported.

`Reference/availability_dashboard/availability_repository.py:27` hardcodes the
`\\sandc.ws\...` UNC path and ignores its `db_path` argument. It must route
through `_configured_db_path()` / `GREMLIN_DB_PATH` like `get_life_data_service()`
does, or the card would read a different database than every other page.

**Availability is the Metrics page's first request** — it also supplies the
equipment list the other cards default to (§2.7) — so it cannot assume another
endpoint has already built `LifeDataService`. `api_availability` therefore
bootstraps it, which does two things nothing else would:

- `ensure_schema` adds columns a database predating a migration is missing. A
  long-lived GREMLIN.db only ever gains columns, so meeting one mid-migration is
  a real state, not a hypothetical.
- `ensure_mapped_records_available` maps raw rows when `mapped_cmms_record` is
  still empty — the state right after an import.

Without it, the first visit after an upgrade 500s on a missing column and the
first visit after an import reports "no work orders imported" while the raw rows
sit there unmapped. Neither is retried once a later request repairs things, so
the card stays wrong for the whole page view.

As defence in depth the work-order query is also schema-adaptive: it selects
only the columns `PRAGMA table_info` reports, substituting `NULL` for the rest,
so a missing column degrades to the fallbacks those fields already have instead
of raising. `RawRepository` is deliberately schema-aware for the same reason.

## 6. Known gaps

- **Weekends are hardcoded.** Any group that genuinely runs 7 days has its
  availability overstated, with no way to express it. An `include_weekends` flag
  is the fix; `asset_schedule_class.exclude_weekends` already exists in the DB as
  precedent.
- **No holiday or shutdown calendar.** A shutdown week counts as scheduled hours
  the plant never intended to run. `schedule_exception` already exists in the DB
  (asset, start/end, type) and is unused. Out of scope for v1, but the calculator
  should be shaped so subtracting exception hours is a drop-in later.
- **No authorization.** Anyone who can reach the app can change any config. The
  dev PIN guards only the developer dashboard.
- **Concurrent edits are last-write-wins.** Acceptable at this edit frequency.
  Writes go through `write_connection`, which converts SQLite and OS failures
  via the shared `database_write_error` helper, so "another user is saving",
  "the share is read-only" and "the drive is full" reach the user as the same
  actionable 503 the rest of GREMLIN produces rather than as a bare
  `database is locked` 500.

## 7. Tests

Golden values taken from the workbook so "matches the spreadsheet" is objective:

| Case | Expected |
|---|---|
| 3101 Jan-26 scheduled | 22 weekdays × 18 h = 396 h |
| 3101 Jan-26 availability | 10.0 h downtime → 97.4747% |
| 3102 Jan-26 linked | 24.43 h linked, 36.93 h adjusted → 90.6734% |
| Salvagnini Jan-26 Average | 95.0487% |
| Goal, all groups | 0.95 |

Plus regression guards for the decisions above:

- A work order whose `completionNotes` contain `"RTS at 4:30 PM"` **is counted**
  (guards §2.1 — this is the 623.7 h failure).
- A `type = 1` PM carrying non-zero downtime **is counted**.
- Adjusted downtime exceeding scheduled hours clamps to 0% and sets `Flagged`.
- An asset with no work orders reports 100% **and** its no-entries note.
- The current partial month is absent from the window.
- The default window is the 5 most recent complete months, and clamps to fewer
  when the database holds less history.

Existing tests build temp-file SQLite databases (`tests/test_downtime.py`); the
same pattern applies.

Test work orders are seeded as Limble-shaped `raw_json` and mapped by
`LifeDataService`, not written straight into `mapped_cmms_record`. Two reasons:
the mapper re-derives mapped rows whenever its version changes, so hand-written
mapped rows are silently replaced; and going through the real path means the
classifier actually runs, so the return-to-service guard asserts that production
really does mislabel that row as a PM *and* that availability counts it anyway.
