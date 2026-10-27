"""Every analysis on Perform an Analysis, read from the disposition standard.

The page analyses one *population* (spec section 7.1): the work orders an asset
and its sub-assets reach, narrowed by location, component, failure mode,
mechanism, cause and action, by disposition category, and by date. Every view on
the page reads that same population, so a Pareto bar, the trend under it, the
downtime behind it and the Weibull fit of it always count the same records:

* the summary -- readiness counts, the Pareto by any dimension (with causes
  underneath), the location x component matrix, the monthly trend, and the
  rankings of the saved fits;
* downtime drivers, PM effectiveness and repeat fixes;
* the renewal-stream Weibull fit (spec section 5), saved with its population
  definition string, adjusted, and reported.

The records come through ``StandardDispositionService`` and every date, schedule
and fit through ``LifeDataService``, so nothing here has its own idea of when a
work order happened or how a life is measured.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping

from services import disposition_standard as std
from services.standard_disposition_schema import AssetTree, load_codebook
from services.standard_disposition_service import StandardDispositionService, _clean
from services.wo_narrative import NARRATIVE_KEYS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from services.life_data_service import LifeDataService

# Stamped on every saved run, so a result can be traced to the rules that built it.
# Bump it whenever interval construction or the fit changes.
RENEWAL_METHOD_VERSION = "renewal-stream-v1"

# The dimensions a Pareto bar or a matrix axis can be: the record field, the code
# list its values come from (for their names), and what the page calls it.
GROUP_BY_DIMENSIONS = {
    # Where on the asset: the sub-asset a work order was dispositioned to, or the
    # asset itself when it names none.
    "sub_asset": ("location", None, "Sub-asset"),
    "component": ("component_code", std.COMPONENT, "Component"),
    "failure_mode": ("failure_mode_code", std.FAILURE_MODE, "Failure mode"),
    "failure_mechanism": ("failure_mechanism_code", std.FAILURE_MECHANISM, "Failure mechanism"),
    "failure_cause": ("failure_cause_effective", std.FAILURE_CAUSE, "Failure cause"),
    "action": ("action_code", std.ACTION, "Action"),
    "category": ("disposition_category", None, "Disposition category"),
    "detection_method": ("detection_method", None, "Detection method"),
}
# What a click on a bar of each dimension narrows the population by, so the page
# can drill down: the selection key it adds the bar's value to.
GROUP_BY_SELECTION = {
    "sub_asset": "locations",
    "component": "components",
    "failure_mode": "failure_modes",
    "failure_mechanism": "failure_mechanisms",
    "failure_cause": "failure_causes",
    "action": "actions",
    "category": "categories",
}
# The drill-down a Pareto click steps through (spec section 7.2).
DRILL_DOWN = ("sub_asset", "component", "failure_mechanism")
# The Weibull view's Pareto: one bar per population a Weibull is best run on,
# one sub-asset's one component failing by one mechanism.
WEIBULL_PARETO = "weibull_population"
WEIBULL_PARETO_LABEL = "Sub-asset, component and mechanism"
RECORDS_LIMIT = 500
NOT_CODED = "Not coded"

# The downtime histogram's bins: many short stops, or a few long ones.
DOWNTIME_BINS: tuple[tuple[str, float, float | None], ...] = (
    ("0–1 hr", 0.0, 1.0),
    ("1–4 hr", 1.0, 4.0),
    ("4–8 hr", 4.0, 8.0),
    ("8–24 hr", 8.0, 24.0),
    ("24+ hr", 24.0, None),
)
TOP_DOWNTIME_EVENTS = 10
# The trend's growth cards compare the last three months with the three before.
GROWTH_MONTHS = 3
RANKING_LIMIT = 5


def _month(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _month_range(keys: Iterable[str]) -> list[str]:
    ordered = sorted(keys)
    if not ordered:
        return []
    year, month = (int(part) for part in ordered[0].split("-"))
    last = tuple(int(part) for part in ordered[-1].split("-"))
    months = []
    while (year, month) <= last:
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def _selection_key(selection: Mapping[str, Any]) -> str:
    """A saved run's selection, to match runs by.

    The categories narrow the pattern views only -- the life engine has its own
    rules for each category -- so two selections that differ only in them are the
    same Weibull population. A run saved before the Weibull stopped reading mode,
    cause and action keeps them in its key, so it never stands for a population
    picked today (``_life_selection_key``).
    """

    canonical = {key: value for key, value in std.Population.from_mapping(selection).to_mapping().items() if key != "categories"}
    return json.dumps(canonical, sort_keys=True)


def _life_selection_key(selection: Mapping[str, Any]) -> str:
    """The page's selection as the Weibull reads it, without the pattern-only narrowing."""

    return _selection_key(std.Population.from_mapping(selection).for_life_analysis().to_mapping())


# Populations already read (see PopulationAnalysisService._load), least recently used first.
_LOADED: "OrderedDict[tuple, dict[str, Any]]" = OrderedDict()
_LOADED_SIZE = 8
_LOADED_LOCK = threading.Lock()
# The rankings' saved fits (see PopulationAnalysisService._ranked_fits), kept the same way.
_RANKED: "OrderedDict[tuple, list[dict[str, Any]]]" = OrderedDict()


class PopulationAnalysisService:
    """Perform an Analysis, over one ``LifeDataService``."""

    def __init__(self, life: "LifeDataService", records: StandardDispositionService | None = None) -> None:
        self.life = life
        self.records = records or StandardDispositionService(life)

    # ------------------------------------------------------------------ #
    # The population's records
    # ------------------------------------------------------------------ #
    def _scope_rows(self, conn: sqlite3.Connection, population: std.Population, tree: AssetTree) -> list[sqlite3.Row]:
        """Every record the population could reach: its assets, their sub-assets, and its locations.

        A record is in reach when the asset it was logged against, or the location
        it was dispositioned to, is one of them -- so selecting 4002 finds the work
        orders logged on 4002 itself, those logged on 4002-S09, and those logged on
        4002 and placed on S09. With no asset or location, every record is in reach.
        """

        columns, source = self.records.RECORD_COLUMNS, self.records.RECORD_FROM
        reach = set(population.assets) | set(population.locations)
        for asset in population.assets:
            reach.update(option["asset_number"] for option in tree.descendants(asset))
        if not reach:
            return conn.execute(
                f"SELECT {columns} {source} WHERE m.asset_number IS NOT NULL AND TRIM(m.asset_number) <> ''"
            ).fetchall()
        placeholders = ",".join("?" for _ in reach)
        values = sorted(reach)
        # The two ways in, each found through its own index and united, rather
        # than one OR across both tables, which can only be answered by reading
        # every work order.
        return conn.execute(
            f"""
            SELECT {columns} {source}
            WHERE m.mapped_record_id IN (
                SELECT mapped_record_id FROM mapped_cmms_record WHERE TRIM(asset_number) IN ({placeholders})
                UNION
                SELECT mapped_record_id FROM wo_disposition WHERE is_current = 1 AND sub_asset IN ({placeholders})
            )
            """,
            [*values, *values],
        ).fetchall()

    def _linked_completions(self, conn: sqlite3.Connection, record_ids: list[int]) -> dict[int, list[Any]]:
        """For each record, the completed dates of the duplicates that link to it."""

        completions: dict[int, list[Any]] = {}
        for start in range(0, len(record_ids), 500):
            chunk = record_ids[start:start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"""
                SELECT w.linked_mapped_record_id, m.completed_date_final
                FROM wo_disposition w
                JOIN mapped_cmms_record m ON m.mapped_record_id = w.mapped_record_id
                WHERE w.is_current = 1 AND w.disposition_category = 'DUPLICATE_MISFILED'
                  AND w.linked_mapped_record_id IN ({placeholders})
                """,
                chunk,
            ):
                completions.setdefault(int(row[0]), []).append(row[1])
        return completions

    def _population_records(
        self,
        conn: sqlite3.Connection,
        population: std.Population,
        zone: tzinfo,
        codebook: std.CodeBook,
        tree: AssetTree,
    ) -> list[dict[str, Any]]:
        """The records in reach, each with its derived fields and the dates the analyses use.

        ``event_date`` is when the work order says the event happened (created, else
        started, else completed); ``renewal_date`` is when the item was renewed (its
        completion, or that of the last repair work order linked to it).
        """

        rows = self._scope_rows(conn, population, tree)
        linked = self._linked_completions(conn, [int(row["mapped_record_id"]) for row in rows])
        records = []
        for row in rows:
            record = self.records.record_dict(row, codebook)
            disposition = record["disposition"]
            created = self.life._parse_event_datetime(row["created_date_final"], zone)
            started = self.life._parse_event_datetime(row["start_date_final"], zone)
            completed = self.life._parse_event_datetime(row["completed_date_final"], zone)
            event_date = created or started or completed
            renewal_date = completed
            links = linked.get(record["mapped_record_id"])
            if links is not None:
                # Diagnosed now, repaired later (spec section 6): the item is renewed
                # when the last repair work order linked to it is finished, and not
                # before -- an open one means the repair is not done.
                dates = [self.life._parse_event_datetime(value, zone) for value in links]
                renewal_date = (
                    max([*dates, *([completed] if completed else [])])
                    if dates and all(when is not None for when in dates)
                    else None
                )
            record.update({
                "category": disposition["disposition_category"],
                "location": record["derived"]["location"],
                "location_label": self._place_label(tree, population, record["derived"]["location"]),
                "event_date": event_date,
                "renewal_date": renewal_date,
                "event_day": event_date.astimezone(zone).date() if event_date else None,
            })
            records.append(record)
        return records

    @staticmethod
    def _place_label(tree: AssetTree, population: std.Population, number: str | None) -> str:
        """Where a work order is, as the page names it: a sub-asset by its name, or
        the selected asset itself, for one placed on no sub-asset."""

        if not number:
            return "Not coded"
        if number in population.assets:
            return f"{tree.label(number)}, no sub-asset"
        return tree.label(number)

    def _load(self, conn: sqlite3.Connection, population: std.Population) -> dict[str, Any]:
        """The population's records and what they were read with: codes, plant zone, asset tree.

        Called first in its transaction. Reading an asset's records is most of
        what every view costs, and the page asks for several views of one asset
        at a time (the selector, the summary, the open tab, each filter changed),
        so what was read is kept, per asset and location reach, until GREMLIN.db
        changes: the change token is read before the transaction's first query,
        so the records kept under it are never older than it says. Nothing that
        reads the result changes it.
        """

        token = self.life.change_token()
        codebook = load_codebook(conn)
        zone, zone_name, zone_warning = self.life._plant_time_zone(conn)
        tree = self.records.asset_tree(conn, token=token)
        key = None
        if token is not None:
            key = (token, tree.serial, zone_name, tuple(sorted(population.assets)), tuple(sorted(population.locations)))
            with _LOADED_LOCK:
                loaded = _LOADED.get(key)
                if loaded is not None:
                    _LOADED.move_to_end(key)
                    return loaded
        records = self._population_records(conn, population, zone, codebook, tree)
        for record in records:
            record["induced"] = codebook.has_semantic(
                std.FAILURE_CAUSE, record["derived"]["failure_cause_effective"], std.SEMANTIC_INDUCED
            )
        loaded = {
            "codebook": codebook,
            "zone": zone,
            "zone_name": zone_name,
            "zone_warning": zone_warning,
            "records": records,
            "tree": tree,
            "places": {record["location"]: record["location_label"] for record in records},
            "token": token,
        }
        if key is not None:
            with _LOADED_LOCK:
                _LOADED[key] = loaded
                while len(_LOADED) > _LOADED_SIZE:
                    _LOADED.popitem(last=False)
        return loaded

    @staticmethod
    def _pattern_value(record: Mapping[str, Any], field: str) -> Any:
        """A record's value on one dimension, as the pattern views read it.

        A HELD record shows in the pattern views as unknown (spec section 2.1):
        reviewed, but with nothing it says that an analysis can rest on, so its
        coded fields read as not coded whatever was entered.
        """

        disposition = record["disposition"]
        if field == "asset_number":
            return record["asset_number"]
        if field == "location":
            return record["location"]
        if field == "disposition_category":
            return record["category"]
        if field in ("failure_cause_effective", "detection_method"):
            value = record["derived"][field]
        else:
            value = disposition.get(field)
        if record["category"] == std.HELD and field != "detection_method":
            return None
        return value

    def _pattern_records(
        self,
        records: list[dict[str, Any]],
        population: std.Population,
        start: date | None,
        end: date | None,
        *,
        categories: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """The population's records for the pattern views, dated by when the event happened."""

        wanted = frozenset(categories) if categories is not None else (
            population.categories or frozenset(std.PATTERN_VIEW_CATEGORIES)
        )
        selected = []
        for record in records:
            if record["category"] not in wanted:
                continue
            if start is not None and (record["event_day"] is None or record["event_day"] < start):
                continue
            if end is not None and (record["event_day"] is None or record["event_day"] > end):
                continue
            if all(
                not getattr(population, attribute) or self._pattern_value(record, field) in getattr(population, attribute)
                for attribute, field, _ in std.POPULATION_DIMENSIONS
                if attribute != "assets"
            ):
                # Induced failures left out of the Weibull are left out of its patterns too.
                if population.exclude_induced and record["category"] == std.FAILURE and record["induced"]:
                    continue
                selected.append(record)
        return selected

    @staticmethod
    def _label(codebook: std.CodeBook, code_list: str | None, value: Any) -> str:
        if value is None:
            return NOT_CODED
        if code_list is not None:
            name = codebook.name(code_list, value)
            return f"{value} · {name}" if name and name != value else str(value)
        if value in std.CATEGORY_GUIDE:
            return std.CATEGORY_GUIDE[value]["label"]
        if value in std.DETECTION_LABELS:
            return std.DETECTION_LABELS[value]
        return str(value)

    @staticmethod
    def _population_label(population: std.Population, tree: AssetTree) -> str:
        """The population in a few words, for headings and the report: "4002-S09 Dry-off oven · Filter · CONT"."""

        places = population.locations or population.assets
        parts = [", ".join(tree.label(number) for number in sorted(places))] if places else []
        for attribute in ("components", "failure_mechanisms", "failure_modes", "failure_causes", "actions"):
            values = getattr(population, attribute)
            if values:
                parts.append(", ".join(sorted(values)))
        if population.exclude_induced:
            parts.append("induced failures as suspensions")
        return " · ".join(parts) or "Every work order in GREMLIN"

    @staticmethod
    def _check_dates(start: date | None, end: date | None) -> None:
        if start is not None and end is not None and start > end:
            raise ValueError("The From date can't be later than the To date.")

    # ------------------------------------------------------------------ #
    # Selector options
    # ------------------------------------------------------------------ #
    def options(self, assets: Iterable[str]) -> dict[str, Any]:
        """The values each selector dimension takes among the records in reach of ``assets``."""

        population = std.Population(assets=frozenset(text for asset in assets if (text := _clean(asset))))
        with self.life.read_transaction() as conn:
            loaded = self._load(conn, population)
        codebook, records, tree = loaded["codebook"], loaded["records"], loaded["tree"]
        sub_assets = {asset: tree.descendants(asset) for asset in sorted(population.assets)}
        places = dict(loaded["places"])
        dimensions: dict[str, list[dict[str, Any]]] = {}
        for attribute, field, _ in std.POPULATION_DIMENSIONS:
            if attribute == "assets":
                continue
            code_list = next((dimension[1] for dimension in GROUP_BY_DIMENSIONS.values() if dimension[0] == field), None)
            counts: dict[Any, int] = {}
            for record in records:
                if record["category"] in (std.NOT_REVIEWED, std.DUPLICATE_MISFILED):
                    continue
                value = self._pattern_value(record, field)
                if value is not None:
                    counts[value] = counts.get(value, 0) + 1
            values = set(counts)
            if attribute == "locations":
                values |= set(population.assets)
                for options in sub_assets.values():
                    values |= {option["asset_number"] for option in options}
            if attribute == "locations":
                for value in values:
                    places.setdefault(value, self._place_label(tree, population, value))
            ordered = sorted(values, key=lambda value: (-counts.get(value, 0), str(value)))
            depth: dict[Any, int] = {}
            if attribute == "locations":
                # Where on the asset, as its hierarchy reads: the asset itself, then
                # each sub-asset under its parent; anything else after them.
                tree_order = []
                for asset in sorted(population.assets):
                    tree_order.append(asset)
                    depth[asset] = 0
                    for option in sub_assets[asset]:
                        tree_order.append(option["asset_number"])
                        depth[option["asset_number"]] = option["depth"]
                ordered = [value for value in tree_order if value in values] + [
                    value for value in ordered if value not in depth
                ]
            dimensions[attribute] = [
                {
                    "value": value,
                    "label": places[value] if attribute == "locations" else self._label(codebook, code_list, value),
                    "count": counts.get(value, 0),
                    **({"depth": depth.get(value, 0)} if attribute == "locations" else {}),
                }
                for value in ordered
            ]
        return {
            "assets": sorted(population.assets),
            "asset_labels": {asset: tree.label(asset) for asset in population.assets},
            "sub_assets": [option for options in sub_assets.values() for option in options],
            "dimensions": dimensions,
            "categories": [
                {"value": code, "label": std.CATEGORY_GUIDE[code]["label"], "default": code == std.FAILURE}
                for code in std.DISPOSITION_CATEGORIES
                if code not in (std.NOT_REVIEWED, std.DUPLICATE_MISFILED)
            ],
            "group_by": [{"value": key, "label": value[2]} for key, value in GROUP_BY_DIMENSIONS.items()],
            "group_by_selection": dict(GROUP_BY_SELECTION),
            "drill_down": list(DRILL_DOWN),
            "record_count": len(records),
        }

    # ------------------------------------------------------------------ #
    # Summary: readiness, Pareto, matrix, trend and rankings
    # ------------------------------------------------------------------ #
    def summary(
        self,
        selection: Mapping[str, Any],
        *,
        start: date | None = None,
        end: date | None = None,
        group_by: str = "sub_asset",
        weeks: float = 4,
        weibull: bool = False,
    ) -> dict[str, Any]:
        """What the page draws for a population before any one analysis is opened.

        ``weibull`` is the Weibull view's: the population as the fit reads it
        (no mode, cause, action or categories), its failures only, and a Pareto
        bar per sub-asset, component and mechanism whatever ``group_by`` says.
        """

        self._check_dates(start, end)
        if not weibull and group_by not in GROUP_BY_DIMENSIONS:
            raise ValueError(f"Group the Pareto by one of: {', '.join(GROUP_BY_DIMENSIONS)}.")
        population = std.Population.from_mapping(selection)
        if weibull:
            population = population.for_life_analysis()
        with self.life.read_transaction() as conn:
            loaded = self._load(conn, population)
            fits = self._ranked_fits(conn, population, loaded["tree"], token=loaded["token"])
        codebook, records = loaded["codebook"], loaded["records"]
        selected = self._pattern_records(records, population, start, end, categories=[std.FAILURE] if weibull else None)
        window_start = start or min((record["event_day"] for record in selected if record["event_day"]), default=None)
        window_end = end or datetime.now(loaded["zone"]).date()
        return {
            "selection": population.to_mapping(),
            "population_label": self._population_label(population, loaded["tree"]),
            "population_definition": std.population_definition(population, window_start, window_end),
            "readiness": self._readiness(records, population, start, end, codebook),
            "record_count": len(selected),
            "downtime_hours": round(sum(float(record["downtime_hours"] or 0) for record in selected), 4),
            "pareto": self._weibull_pareto(selected, codebook) if weibull else self._pareto(selected, group_by, codebook),
            "matrix": self._matrix(selected, codebook),
            "trend": None if weibull else self._trend(
                selected, group_by, codebook, own=self._own_buckets(population, group_by, loaded["tree"]),
            ),
            "rankings": self._beta_rankings(fits),
            "risk_rankings": self._risk_rankings(fits, weeks),
            "plant_time_zone": loaded["zone_name"],
        }

    def _readiness(
        self,
        records: list[dict[str, Any]],
        population: std.Population,
        start: date | None,
        end: date | None,
        codebook: std.CodeBook,
    ) -> dict[str, int]:
        """How far the records in reach have got: what the Weibull can use, and what still needs a look."""

        uncoded = sum(
            1
            for record in records
            if record["category"] in std.RENEWING_CATEGORIES
            and not std.stream_touches(self._stream_event(record, codebook))
        )
        return {
            "in_reach": len(records),
            "dispositioned": sum(1 for record in records if record["category"] != std.NOT_REVIEWED),
            "not_reviewed": sum(1 for record in records if record["category"] == std.NOT_REVIEWED),
            "to_confirm": sum(1 for record in records if record["disposition"]["source"] == "MIGRATION"),
            # Saved by the disposition helper's batch run and not yet confirmed.
            "auto_unconfirmed": sum(1 for record in records if record["disposition"]["source"] == "AUTO"),
            "failures": len(self._pattern_records(records, population, start, end, categories=[std.FAILURE])),
            "pm_resets": len(self._pattern_records(records, population, start, end, categories=[std.PM_RESET])),
            "uncoded": uncoded,
        }

    def _value_label(self, record: Mapping[str, Any], field: str, code_list: str | None, codebook: std.CodeBook) -> str:
        """A record's value on one dimension, named: a sub-asset by its name, a code with its name."""

        if field == "location":
            return record["location_label"]
        return self._label(codebook, code_list, self._pattern_value(record, field))

    def _pareto(self, records: list[dict[str, Any]], group_by: str, codebook: std.CodeBook) -> dict[str, Any]:
        """One bar per value the population's work orders are coded with on ``group_by``.

        Work orders not coded on it make no bar -- "Not coded" is not a component,
        a mechanism or a cause, and as the tallest bar it hid the ones that are --
        and are counted in ``not_coded`` instead, so the page can say how many.
        """

        field, code_list, label = GROUP_BY_DIMENSIONS[group_by]
        rows, not_coded = self._bars(
            records,
            lambda record: self._pattern_value(record, field),
            lambda record: self._value_label(record, field, code_list, codebook),
            codebook,
        )
        next_level = None
        if group_by in DRILL_DOWN and DRILL_DOWN.index(group_by) + 1 < len(DRILL_DOWN):
            next_level = DRILL_DOWN[DRILL_DOWN.index(group_by) + 1]
        return {
            "group_by": group_by,
            "label": label,
            "selection_key": GROUP_BY_SELECTION.get(group_by),
            "next_group_by": next_level,
            "rows": rows,
            "not_coded": not_coded,
        }

    def _weibull_pareto(self, records: list[dict[str, Any]], codebook: std.CodeBook) -> dict[str, Any]:
        """One bar per sub-asset, component and mechanism the population's failures are coded with.

        Each is the population a Weibull is best run on, and carries the selection
        that runs it. A failure with no component or mechanism makes no bar. When
        every bar is on the one place -- an asset with no sub-assets, or a
        population narrowed to one -- the names leave it out and start at the
        component.
        """

        def key_of(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
            component = self._pattern_value(record, "component_code")
            mechanism = self._pattern_value(record, "failure_mechanism_code")
            if record["location"] is None or component is None or mechanism is None:
                return None
            return (record["location"], component, mechanism)

        places: dict[str, str] = {}

        def label_of(record: Mapping[str, Any]) -> str:
            location, component, mechanism = key_of(record)
            places.setdefault(location, record["location_label"])
            return f"{location} · {component} · {mechanism}"

        rows, not_coded = self._bars(records, key_of, label_of, codebook)
        one_place = len(places) == 1
        for row in rows:
            location, component, mechanism = row["key"]
            if one_place:
                row["label"] = f"{component} · {mechanism}"
            row["key"] = " | ".join(row["key"])
            row["selection"] = {"locations": [location], "components": [component], "failure_mechanisms": [mechanism]}
            row["parts"] = [
                {"label": "Sub-asset", "value": places[location]},
                {"label": "Component", "value": self._label(codebook, std.COMPONENT, component)},
                {"label": "Failure mechanism", "value": self._label(codebook, std.FAILURE_MECHANISM, mechanism)},
            ]
        return {
            "group_by": WEIBULL_PARETO,
            "label": WEIBULL_PARETO_LABEL,
            "selection_key": None,
            "next_group_by": None,
            "rows": rows,
            "not_coded": not_coded,
        }

    def _bars(
        self,
        records: list[dict[str, Any]],
        key_of: Callable[[Mapping[str, Any]], Any],
        label_of: Callable[[Mapping[str, Any]], str],
        codebook: std.CodeBook,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The Pareto's bars, biggest first, each split by failure cause, and what made none."""

        groups: dict[Any, dict[str, Any]] = {}
        labels: dict[Any, str] = {}
        not_coded = {"count": 0, "downtime_hours": 0.0}
        for record in records:
            key = key_of(record)
            if key is None:
                not_coded["count"] += 1
                not_coded["downtime_hours"] += float(record["downtime_hours"] or 0)
                continue
            labels.setdefault(key, label_of(record))
            cause = self._pattern_value(record, "failure_cause_effective")
            downtime = float(record["downtime_hours"] or 0)
            group = groups.setdefault(key, {"count": 0, "downtime_hours": 0.0, "causes": {}})
            group["count"] += 1
            group["downtime_hours"] += downtime
            by_cause = group["causes"].setdefault(cause, {"count": 0, "downtime_hours": 0.0})
            by_cause["count"] += 1
            by_cause["downtime_hours"] += downtime
        rows = [
            {
                "key": key,
                "label": labels[key],
                "count": group["count"],
                "downtime_hours": round(group["downtime_hours"], 4),
                "causes": [
                    {
                        "key": cause,
                        "label": self._label(codebook, std.FAILURE_CAUSE, cause),
                        "count": values["count"],
                        "downtime_hours": round(values["downtime_hours"], 4),
                    }
                    for cause, values in sorted(group["causes"].items(), key=lambda item: (-item[1]["count"], str(item[0])))
                ],
            }
            for key, group in groups.items()
        ]
        rows.sort(key=lambda row: (-row["count"], -row["downtime_hours"], str(row["label"])))
        return rows, {"count": not_coded["count"], "downtime_hours": round(not_coded["downtime_hours"], 4)}

    def _matrix(self, records: list[dict[str, Any]], codebook: std.CodeBook) -> dict[str, Any]:
        """Event counts by sub-asset (rows) and component (columns)."""

        cells: dict[tuple[Any, Any], dict[str, float]] = {}
        locations: dict[Any, int] = {}
        location_labels: dict[Any, str] = {}
        components: dict[Any, int] = {}
        for record in records:
            location = record["location"]
            location_labels.setdefault(location, record["location_label"])
            component = self._pattern_value(record, "component_code")
            cell = cells.setdefault((location, component), {"count": 0, "downtime_hours": 0.0})
            cell["count"] += 1
            cell["downtime_hours"] += float(record["downtime_hours"] or 0)
            locations[location] = locations.get(location, 0) + 1
            components[component] = components.get(component, 0) + 1
        location_keys = sorted(locations, key=lambda key: self.life._natural_key(location_labels[key]))
        component_keys = sorted(components, key=lambda key: (-components[key], str(key)))
        return {
            "rows": [{"key": key, "label": location_labels[key], "total": locations[key]} for key in location_keys],
            "columns": [
                {"key": key, "label": self._label(codebook, std.COMPONENT, key), "total": components[key]}
                for key in component_keys
            ],
            "cells": [
                [
                    {
                        "count": int(cells.get((row, column), {}).get("count", 0)),
                        "downtime_hours": round(cells.get((row, column), {}).get("downtime_hours", 0.0), 4),
                    }
                    for column in component_keys
                ]
                for row in location_keys
            ],
            "max_count": max((int(cell["count"]) for cell in cells.values()), default=0),
        }

    @staticmethod
    def _own_buckets(population: std.Population, group_by: str, tree: AssetTree) -> dict[str, str]:
        """The values of the Pareto's dimension that are the asset itself, by label.

        Grouped by sub-asset, a work order placed on no sub-asset is counted under
        the asset it was logged against. That is where every work order nobody
        placed lands, so it is the biggest group on almost any asset, and a card
        naming it says only that much of the history is not placed. The cards
        compare the sub-assets instead; the Pareto still draws the asset's bar.
        """

        if GROUP_BY_DIMENSIONS[group_by][0] != "location":
            return {}
        return {number: tree.label(number) for number in population.assets}

    def _trend(
        self,
        records: list[dict[str, Any]],
        group_by: str,
        codebook: std.CodeBook,
        *,
        own: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Monthly counts and downtime for the population, and per value of the Pareto's dimension.

        The month axis is continuous, so a quiet month is a zero rather than a gap.
        ``growth`` compares the last three months with the three before, and only
        once there are six months to compare.

        The summary cards compare the values the Pareto draws, less the ones
        ``own`` names (the asset itself, grouped by sub-asset; see
        ``_own_buckets``). When nothing is left to compare -- an asset with no
        sub-assets, grouped by sub-asset -- ``summary["note"]`` says why.
        """

        field, code_list, label = GROUP_BY_DIMENSIONS[group_by]
        dated = [record for record in records if record["event_day"] is not None]
        months = _month_range({_month(record["event_day"]) for record in dated})
        index = {key: position for position, key in enumerate(months)}
        counts = [0] * len(months)
        downtime = [0.0] * len(months)
        groups: dict[Any, dict[str, Any]] = {}
        for record in dated:
            slot = index[_month(record["event_day"])]
            hours = float(record["downtime_hours"] or 0)
            counts[slot] += 1
            downtime[slot] += hours
            key = self._pattern_value(record, field)
            group = groups.setdefault(key, {
                "key": key,
                "label": self._value_label(record, field, code_list, codebook),
                "monthly_counts": [0] * len(months),
                "total_count": 0,
                "total_downtime_hours": 0.0,
            })
            group["monthly_counts"][slot] += 1
            group["total_count"] += 1
            group["total_downtime_hours"] += hours
        has_growth_window = len(months) >= 2 * GROWTH_MONTHS
        for group in groups.values():
            group["total_downtime_hours"] = round(group["total_downtime_hours"], 4)
            group["growth"] = (
                sum(group["monthly_counts"][-GROWTH_MONTHS:])
                - sum(group["monthly_counts"][-2 * GROWTH_MONTHS:-GROWTH_MONTHS])
                if has_growth_window
                else None
            )
        ranked = sorted(groups.values(), key=lambda group: (-group["total_count"], str(group["label"])))
        # The cards compare the values the Pareto draws, so work orders not coded
        # on its dimension are left out of them as they are out of its bars, and
        # so is the asset's own bucket (see _own_buckets).
        own = own or {}
        coded = [group for group in ranked if group["key"] is not None and group["key"] not in own]

        def entry(group: dict[str, Any] | None, value: Any) -> dict[str, Any] | None:
            return None if group is None else {"key": group["key"], "label": group["label"], "value": value}

        summary: dict[str, Any] = {
            "most_frequent": None,
            "highest_downtime": None,
            "fastest_growing": None,
            "most_improved": None,
            "note": None,
        }
        if not coded and any(group["key"] in own for group in ranked):
            names = ", ".join(sorted(own[key] for key in {group["key"] for group in ranked} & set(own)))
            summary["note"] = (
                f"Every work order here is on {names} itself rather than on one of its sub-assets, so there are no "
                "sub-assets to compare. Group the Pareto by component, mechanism or mode to compare those."
            )
        if coded:
            most = max(coded, key=lambda group: (group["total_count"], group["total_downtime_hours"]))
            summary["most_frequent"] = entry(most, most["total_count"])
            # With no downtime recorded at all, every value ties at zero and the
            # card would name one at random.
            costly = max(coded, key=lambda group: (group["total_downtime_hours"], group["total_count"]))
            if costly["total_downtime_hours"] > 0:
                summary["highest_downtime"] = entry(costly, costly["total_downtime_hours"])
            growing = [group for group in coded if (group["growth"] or 0) > 0]
            if growing:
                fastest = max(growing, key=lambda group: (group["growth"], group["total_count"]))
                summary["fastest_growing"] = entry(fastest, fastest["growth"])
            declining = [group for group in coded if (group["growth"] or 0) < 0]
            if declining:
                improved = min(declining, key=lambda group: (group["growth"], -group["total_count"]))
                summary["most_improved"] = entry(improved, improved["growth"])
        # Reversed, so the dated come first, newest first, and the undated last.
        newest_first = sorted(
            records,
            key=lambda record: (record["event_date"] is not None, record["event_date"] or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
        return {
            "group_by": group_by,
            "label": label,
            "months": months,
            "monthly_counts": counts,
            "monthly_downtime_hours": [round(value, 4) for value in downtime],
            "groups": ranked,
            "has_growth_window": has_growth_window,
            "summary": summary,
            "undated": len(records) - len(dated),
            "records": [self._record_summary(record, codebook) for record in newest_first[:RECORDS_LIMIT]],
            "records_truncated": len(records) > RECORDS_LIMIT,
        }

    def _record_summary(self, record: Mapping[str, Any], codebook: std.CodeBook) -> dict[str, Any]:
        disposition = record["disposition"]
        return {
            "mapped_record_id": record["mapped_record_id"],
            "task_id": record["task_id"],
            "task_name": record["task_name"],
            "asset_number": record["asset_number"],
            "event_date": record["event_day"].isoformat() if record["event_day"] else None,
            "month": _month(record["event_day"]) if record["event_day"] else None,
            "category": record["category"],
            "location": record["location"],
            "location_label": record["location_label"],
            "component": self._label(codebook, std.COMPONENT, disposition["component_code"]),
            "failure_mode": self._label(codebook, std.FAILURE_MODE, disposition["failure_mode_code"]),
            "failure_mechanism": self._label(codebook, std.FAILURE_MECHANISM, disposition["failure_mechanism_code"]),
            "failure_cause": self._label(codebook, std.FAILURE_CAUSE, record["derived"]["failure_cause_effective"]),
            "action": self._label(codebook, std.ACTION, disposition["action_code"]),
            "qty": disposition["qty"],
            "downtime_hours": record["downtime_hours"],
            "requestor_description": record["requestor_description"],
            "completion_notes": record["completion_notes"],
            **{key: record[key] for key in NARRATIVE_KEYS},
        }

    # ------------------------------------------------------------------ #
    # Downtime drivers
    # ------------------------------------------------------------------ #
    def downtime(self, selection: Mapping[str, Any], *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        """Where the population's downtime comes from: when, in what size of stop, where, and which work orders."""

        self._check_dates(start, end)
        population = std.Population.from_mapping(selection)
        with self.life.read_transaction() as conn:
            loaded = self._load(conn, population)
        codebook = loaded["codebook"]
        selected = self._pattern_records(loaded["records"], population, start, end)
        values = [max(0.0, float(record["downtime_hours"] or 0)) for record in selected]
        total = sum(values)
        bins = [0] * len(DOWNTIME_BINS)
        by_location: dict[str, float] = {}
        monthly: dict[str, float] = {}
        for record, hours in zip(selected, values):
            for position, (_, lower, upper) in enumerate(DOWNTIME_BINS):
                if hours >= lower and (upper is None or hours < upper):
                    bins[position] += 1
                    break
            by_location[record["location_label"]] = by_location.get(record["location_label"], 0.0) + hours
            if record["event_day"] is not None:
                key = _month(record["event_day"])
                monthly[key] = monthly.get(key, 0.0) + hours
        months = _month_range(monthly)
        top = sorted(
            zip(selected, values),
            key=lambda item: (item[1], item[0]["event_date"] or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )[:TOP_DOWNTIME_EVENTS]
        count = len(selected)
        return {
            "selection": population.to_mapping(),
            "summary": {
                "total_downtime_hours": round(total, 4),
                "work_order_count": count,
                "average_downtime_hours": round(total / count, 4) if count else 0.0,
                "median_downtime_hours": round(self.life._median(values), 4),
                "max_downtime_hours": round(max(values), 4) if values else 0.0,
            },
            "months": months,
            "monthly_downtime_hours": [round(monthly.get(key, 0.0), 4) for key in months],
            "distribution": [{"label": label, "count": bins[position]} for position, (label, _, _) in enumerate(DOWNTIME_BINS)],
            "by_location": [
                {"label": label, "downtime_hours": round(hours, 4)}
                for label, hours in sorted(by_location.items(), key=lambda item: (-item[1], self.life._natural_key(item[0])))
            ],
            "top_events": [self._record_summary(record, codebook) for record, _ in top],
            "has_records": count > 0,
        }

    # ------------------------------------------------------------------ #
    # PM effectiveness
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pm_rating(average_days: float | None) -> str:
        if average_days is None:
            return "Insufficient Data"
        if average_days >= 180:
            return "Excellent"
        if average_days >= 90:
            return "Good"
        if average_days >= 30:
            return "Fair"
        return "Poor"

    def _is_pm(self, record: Mapping[str, Any], population: std.Population) -> bool:
        """Whether a record is a PM on the population: planned work at one of its locations, on one of its items.

        A PM_RESET counts when it renewed one of the population's components; a PM
        that renewed nothing (dispositioned NON_FAILURE, or not reviewed yet) counts
        as planned work at that location when its record class says PM and it is
        not coded to another component.
        """

        if population.locations and record["location"] not in population.locations:
            return False
        disposition = record["disposition"]
        if record["category"] == std.PM_RESET:
            return not population.components or bool(set(disposition["renewed_items"]) & set(population.components))
        if record["category"] not in (std.NON_FAILURE, std.NOT_REVIEWED):
            return False
        if (record["record_class"] or "").upper() not in std.PM_RECORD_CLASSES:
            return False
        component = disposition["component_code"]
        return not population.components or component is None or component in population.components

    def pm_effectiveness(self, selection: Mapping[str, Any], *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
        """How long after a PM the population's next failure comes, location by location.

        Each completed PM is paired with the first failure of the population at the
        same location found after it. The table and the average are per PM; the
        failure count and the monthly chart count each failure once, however many
        PMs it followed.
        """

        self._check_dates(start, end)
        population = std.Population.from_mapping(selection)
        with self.life.read_transaction() as conn:
            loaded = self._load(conn, population)
        codebook, zone = loaded["codebook"], loaded["zone"]
        failures = [
            record for record in self._pattern_records(loaded["records"], population, start, end, categories=[std.FAILURE])
            if record["event_date"] is not None
        ]
        pms = []
        for record in loaded["records"]:
            when = record["renewal_date"] or (record["event_date"] if record["category"] != std.NOT_REVIEWED else None)
            if when is None or not self._is_pm(record, population):
                continue
            day = when.astimezone(zone).date()
            if (start is not None and day < start) or (end is not None and day > end):
                continue
            pms.append((when, record))
        pms.sort(key=lambda item: (item[0], item[1]["mapped_record_id"]))
        by_location: dict[str, list[dict[str, Any]]] = {}
        for record in sorted(failures, key=lambda record: (record["event_date"], record["mapped_record_id"])):
            by_location.setdefault(record["location"], []).append(record)
        # Each location's failure dates in order, so a PM's next failure is a bisection.
        dates_at = {location: [record["event_date"] for record in found] for location, found in by_location.items()}

        rows = []
        days_values = []
        distinct: dict[int, dict[str, Any]] = {}
        for when, pm in pms:
            found = by_location.get(pm["location"], [])
            position = bisect.bisect_right(dates_at.get(pm["location"], []), when)
            if position >= len(found):
                continue
            following = found[position]
            days = (following["event_date"] - when).total_seconds() / 86400.0
            days_values.append(days)
            distinct.setdefault(following["mapped_record_id"], following)
            rows.append({
                "pm_task_id": pm["task_id"],
                "pm_mapped_record_id": pm["mapped_record_id"],
                "pm_task_name": pm["task_name"],
                "pm_completion_date": when.astimezone(zone).date().isoformat(),
                "pm_kind": "Reset" if pm["category"] == std.PM_RESET else "Planned work",
                "renewed_items": list(pm["disposition"]["renewed_items"]),
                "location": pm["location"],
                "location_label": pm["location_label"],
                "next_failure_date": following["event_day"].isoformat(),
                "days_to_failure": round(days, 1),
                "failure": " · ".join(
                    self._label(codebook, code_list, following["disposition"][field])
                    for field, code_list in (("component_code", std.COMPONENT), ("failure_mechanism_code", std.FAILURE_MECHANISM))
                ),
                "downtime_hours": round(float(following["downtime_hours"] or 0.0), 4),
                "corrective_wo_number": following["task_id"],
                "corrective_mapped_record_id": following["mapped_record_id"],
            })
        rows.sort(key=lambda row: (row["pm_completion_date"], str(row["pm_task_id"])), reverse=True)
        monthly: dict[str, int] = {}
        for failure in distinct.values():
            key = _month(failure["event_day"])
            monthly[key] = monthly.get(key, 0) + 1
        months = _month_range(monthly)
        average = sum(days_values) / len(days_values) if days_values else None
        return {
            "selection": population.to_mapping(),
            "pms_performed": len(pms),
            "pm_resets": sum(1 for _, pm in pms if pm["category"] == std.PM_RESET),
            "failures": len(failures),
            "failures_after_pm": len(distinct),
            "average_days_to_failure": round(average, 1) if average is not None else None,
            "effectiveness": self._pm_rating(average),
            "months": months,
            "monthly_counts": [monthly.get(key, 0) for key in months],
            "rows": rows,
            "has_pm_history": bool(pms),
        }

    # ------------------------------------------------------------------ #
    # Repeat fixes
    # ------------------------------------------------------------------ #
    def repeat_fixes(
        self,
        selection: Mapping[str, Any],
        *,
        window_hours: float = 24.0,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        """How often the population's failures come straight back after the fix, item by item.

        Failures are taken stream by stream (location + component, the item that
        was fixed). A failure is a repeat when it was found within ``window_hours``
        scheduled hours of the previous failure's repair being finished, on that
        location's Weibull schedule as a life is counted: the fix did not hold. The
        rate is repeats over the gaps between failures. Report-only: nothing here
        changes the Weibull lives.
        """

        from services.life_data_service import REPEAT_FIX_MAX_WINDOW_HOURS, REPEAT_FIX_MIN_INTERVALS

        try:
            window = float(window_hours)
        except (TypeError, ValueError):
            raise ValueError("The repeat window has to be a number of hours.") from None
        if not math.isfinite(window) or window < 0 or window > REPEAT_FIX_MAX_WINDOW_HOURS:
            raise ValueError(f"The repeat window has to be between 0 and {REPEAT_FIX_MAX_WINDOW_HOURS:g} hours.")
        self._check_dates(start, end)
        population = std.Population.from_mapping(selection)
        with self.life.read_transaction() as conn:
            loaded = self._load(conn, population)
            failures = self._pattern_records(loaded["records"], population, start, end, categories=[std.FAILURE])
            coded = [record for record in failures if record["disposition"]["component_code"]]
            schedules = self._stream_schedules(conn, loaded["records"], {record["location"] for record in coded}, loaded["tree"])
        codebook, zone, places = loaded["codebook"], loaded["zone"], loaded["places"]
        now = datetime.now(timezone.utc)
        streams: dict[tuple[str, str], list[dict[str, Any]]] = {}
        undated = future = 0
        for record in coded:
            if record["event_date"] is None:
                undated += 1
                continue
            if record["event_date"] > now:
                future += 1
                continue
            streams.setdefault((record["location"], record["disposition"]["component_code"]), []).append(record)

        groups = []
        pairs = []
        for (location, component), events in sorted(streams.items()):
            events.sort(key=lambda record: (record["event_date"], record["mapped_record_id"]))
            schedule = schedules[location]
            label = f"{places.get(location, location)} · {component}"
            repeats = 0
            for prior, repeat in zip(events, events[1:]):
                fixed = prior["renewal_date"] or prior["event_date"]
                scheduled = self.life._scheduled_life_hours(
                    fixed, repeat["event_date"], schedule["hours_per_day"],
                    exclude_weekends=schedule["exclude_weekends"], tz=zone,
                )[0]
                if scheduled > window:
                    continue
                repeats += 1
                raw = (repeat["event_date"] - fixed).total_seconds() / 3600.0
                pairs.append({
                    "group_key": f"{location}|{component}",
                    "label": label,
                    "location": location,
                    "component": component,
                    "prior_task_id": prior["task_id"],
                    "prior_mapped_record_id": prior["mapped_record_id"],
                    "prior_task_name": prior["task_name"],
                    "prior_fixed": fixed.isoformat(),
                    "prior_mechanism": self._label(codebook, std.FAILURE_MECHANISM, prior["disposition"]["failure_mechanism_code"]),
                    "repeat_task_id": repeat["task_id"],
                    "repeat_mapped_record_id": repeat["mapped_record_id"],
                    "repeat_task_name": repeat["task_name"],
                    "repeat_found": repeat["event_date"].isoformat(),
                    "repeat_mechanism": self._label(codebook, std.FAILURE_MECHANISM, repeat["disposition"]["failure_mechanism_code"]),
                    "scheduled_hours": round(scheduled, 2),
                    "raw_hours": round(raw, 2),
                    "duplicate_check": (
                        "Found before the previous repair was finished: check for a duplicate work order"
                        if raw < 0 else self.life._duplicate_check_flag(raw)
                    ),
                })
            intervals = len(events) - 1
            groups.append({
                "key": f"{location}|{component}",
                "label": label,
                "location": location,
                "location_label": places.get(location, location),
                "component": component,
                "schedule_name": schedule["schedule_name"],
                "failures": len(events),
                "intervals": intervals,
                "repeats": repeats,
                "repeat_rate": round(repeats / intervals, 4) if intervals > 0 else None,
            })
        groups.sort(key=lambda group: (-group["repeats"], -(group["repeat_rate"] or 0.0), -group["failures"], group["label"]))
        pairs.sort(key=lambda pair: (pair["repeat_found"], pair["repeat_mapped_record_id"]))
        total_intervals = sum(group["intervals"] for group in groups)
        total_repeats = sum(group["repeats"] for group in groups)
        rated = [group for group in groups if group["intervals"] >= REPEAT_FIX_MIN_INTERVALS and group["repeats"] > 0]
        return {
            "selection": population.to_mapping(),
            "window_hours": window,
            "min_intervals_for_rate": REPEAT_FIX_MIN_INTERVALS,
            "time_zone": loaded["zone_name"],
            "time_zone_warning": loaded["zone_warning"],
            "schedules": sorted({schedule["schedule_name"] for schedule in schedules.values()}),
            "failures": sum(group["failures"] for group in groups),
            "uncoded_failures": len(failures) - len(coded),
            "undated_failures": undated,
            "future_failures": future,
            "intervals": total_intervals,
            "repeats": total_repeats,
            "repeat_rate": round(total_repeats / total_intervals, 4) if total_intervals else None,
            "possible_duplicates": sum(1 for pair in pairs if pair["duplicate_check"]),
            "highest_rate": max(rated, key=lambda group: (group["repeat_rate"], group["repeats"]), default=None),
            "most_repeats": groups[0] if groups and groups[0]["repeats"] > 0 else None,
            "groups": groups,
            "pairs": pairs,
        }

    # ------------------------------------------------------------------ #
    # Renewal-stream Weibull (spec section 5)
    # ------------------------------------------------------------------ #
    def _stream_schedules(
        self,
        conn: sqlite3.Connection,
        records: list[dict[str, Any]],
        locations: Iterable[str],
        tree: AssetTree,
    ) -> dict[str, dict[str, Any]]:
        """The schedule each location's life hours are counted on.

        A sub-asset runs when the asset above it runs, so a location with no entry
        of its own in the Weibull schedule register takes its nearest ancestor's;
        with none, the plant default (REL-WBL-DAT-003 §7).
        """

        locations = sorted(set(locations))
        # A work order placed on a sub-asset also says which asset that is under.
        logged_under: dict[str, str] = {}
        for record in records:
            sub_asset = record["disposition"]["sub_asset"]
            if sub_asset and record["asset_number"]:
                logged_under.setdefault(sub_asset, record["asset_number"])
        registered = {
            str(row[0]).strip(): int(row[1])
            for row in conn.execute("SELECT asset_number, schedule_class_id FROM asset_schedule_assignment")
        }
        classes = {
            int(row["schedule_class_id"]): row
            for row in conn.execute(
                "SELECT schedule_class_id, schedule_class_code, schedule_class_name, hours_per_day, exclude_weekends "
                "FROM asset_schedule_class"
            )
        }
        default_id = self.life._schedule_class_id(conn, "")
        schedules = {}
        for location in locations:
            lineage = [location, *tree.ancestors(location)]
            if location in logged_under and logged_under[location] not in lineage:
                lineage.append(logged_under[location])
            class_id = next((registered[number] for number in lineage if number in registered), default_id)
            row = classes[class_id]
            schedules[location] = {
                "location": location,
                "schedule_code": row["schedule_class_code"],
                "schedule_name": row["schedule_class_name"],
                "hours_per_day": float(row["hours_per_day"]) if row["hours_per_day"] is not None else 24.0,
                "exclude_weekends": bool(row["exclude_weekends"]),
            }
        return schedules

    def _resolve_window(
        self,
        conn: sqlite3.Connection,
        events: list[std.StreamEvent],
        zone: tzinfo,
        analysis_start: date | None,
        analysis_cutoff: date | None,
    ) -> tuple[datetime | None, datetime, str]:
        """(start, cutoff, cutoff source), in UTC.

        A cutoff date runs to the end of that plant day; without one, the last
        completed Limble import when that is no earlier than every event, else now.
        """

        now = datetime.now(timezone.utc).replace(microsecond=0)
        today = now.astimezone(zone).date()
        if analysis_start is not None and analysis_start > today:
            raise ValueError("The analysis start date can't be later than today.")
        if analysis_cutoff is not None:
            if analysis_cutoff > today:
                raise ValueError("The analysis cutoff date can't be later than today.")
            cutoff = min(now, self.life._plant_midnight(analysis_cutoff + timedelta(days=1), zone))
            source = "USER"
        else:
            latest = max(
                (when for event in events for when in (event.event_date, event.renewal_date) if when is not None),
                default=None,
            )
            last_import = self.life._last_completed_import_at(conn)
            if last_import is not None:
                last_import = last_import.replace(microsecond=0)
            if last_import is not None and last_import <= now and (latest is None or last_import >= latest):
                cutoff, source = last_import, "LAST_IMPORT"
            else:
                cutoff, source = now, "NOW"
        start = self.life._plant_midnight(analysis_start, zone) if analysis_start is not None else None
        if start is not None and start >= cutoff:
            raise ValueError("The analysis start date has to be before the cutoff.")
        return start, cutoff, source

    def _stream_event(self, record: Mapping[str, Any], codebook: std.CodeBook) -> std.StreamEvent:
        disposition = record["disposition"]
        mechanism = disposition["failure_mechanism_code"]
        cause = record["derived"]["failure_cause_effective"]
        return std.StreamEvent(
            record_id=record["mapped_record_id"],
            task_id=None if record["task_id"] is None else str(record["task_id"]),
            category=record["category"],
            location=record["location"],
            component=disposition["component_code"],
            mechanism=mechanism,
            mode=disposition["failure_mode_code"],
            cause=cause,
            action=disposition["action_code"],
            renews_item=record["derived"]["renews_item"],
            renewed_items=tuple(disposition["renewed_items"]),
            event_date=record["event_date"],
            renewal_date=record["renewal_date"],
            qty=disposition["qty"],
            mechanism_unknown=mechanism is None or codebook.has_semantic(std.FAILURE_MECHANISM, mechanism, std.SEMANTIC_UNKNOWN),
            cause_induced=codebook.has_semantic(std.FAILURE_CAUSE, cause, std.SEMANTIC_INDUCED),
        )

    def _fit(self, analysis: std.RenewalAnalysis | None, label: str) -> dict[str, Any] | None:
        """The 2P Weibull MLE of one run's lives: the fit, intervals, Kaplan-Meier points, R² and reading."""

        from services.life_data_service import MIN_WEIBULL_FAILURE_LIVES, WeibullFitError

        if analysis is None:
            return None
        data = sorted(analysis.fit_data())
        failures = sum(failed for _, failed in data)
        fit: dict[str, Any] = {
            "label": label,
            "failure_count": failures,
            "suspension_count": len(data) - failures,
            "life_count": len(data),
            "fitted": False,
            "reason": None,
            "min_failure_lives": MIN_WEIBULL_FAILURE_LIVES,
        }
        if failures < MIN_WEIBULL_FAILURE_LIVES:
            fit["reason"] = (
                f"Weibull analysis needs at least {MIN_WEIBULL_FAILURE_LIVES} lives that end in a failure, and this "
                f"population has {failures}. The lives are listed so they can be checked."
            )
            return fit
        try:
            beta, eta, log_likelihood = self.life._fit_weibull_2p(data)
        except WeibullFitError as exc:
            fit["reason"] = str(exc)
            return fit
        beta_lo, beta_hi, eta_lo, eta_hi = self.life._weibull_confidence_intervals(data, beta, eta)
        km_points = self.life._kaplan_meier_points(data)
        r_squared = self.life._probability_plot_r_squared(km_points)
        threshold = self.life.r_squared_review_threshold(failures)
        mean_life = eta * math.gamma(1 + 1 / beta)
        fit.update({
            "fitted": True,
            "beta": beta,
            "eta": eta,
            "beta_lower_ci": beta_lo,
            "beta_upper_ci": beta_hi,
            "eta_lower_ci": eta_lo,
            "eta_upper_ci": eta_hi,
            "log_likelihood": log_likelihood,
            "mean_time_to_failure": mean_life,
            "b10_life": eta * (-math.log(0.90)) ** (1 / beta),
            "b50_life": eta * math.log(2) ** (1 / beta),
            "probability_plot_r_squared": r_squared,
            "probability_plot_r_squared_threshold": threshold,
            "probability_plot_review": bool(r_squared is not None and r_squared < threshold),
            "km_points": km_points,
            "curve_points": self.life._curve_points(beta, eta, max(t for t, _ in data)),
            "interpretation_summary": self.life._weibull_interpretation_summary(
                beta, eta, mean_life, beta_lo, beta_hi, eta_lo, eta_hi, r_squared, failures
            ),
        })
        return fit

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    def _observations(
        self,
        analysis: std.RenewalAnalysis,
        by_id: Mapping[int, Mapping[str, Any]],
        schedules: Mapping[str, Mapping[str, Any]],
        zone: tzinfo,
        cutoff: datetime,
        places: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        """Each life as a row of the Weibull data table, with the work orders that started and ended it."""

        rows = []
        for index, interval in enumerate(
            sorted(analysis.intervals, key=lambda interval: (interval.stream, interval.start)), start=1
        ):
            schedule = schedules[interval.location]
            _, weekend, non_run = self.life._scheduled_life_hours(
                interval.start, interval.end, schedule["hours_per_day"],
                exclude_weekends=schedule["exclude_weekends"], tz=zone,
            )
            if interval.end_type == std.FAILURE_END:
                observation_type = "COMPLETED_FAILURE_LIFE"
            elif interval.end_record_id is None:
                observation_type = "RIGHT_CENSORED_LIFE"
            else:
                observation_type = "SUSPENDED_LIFE"
            source = by_id.get(interval.end_record_id) if interval.end_record_id is not None else None
            starter = by_id.get(interval.start_record_id) if interval.start_record_id is not None else None
            row = {
                "weibull_observation_id": index,
                "ordered_index": index,
                "location": interval.location,
                "component": interval.component,
                "stream": f"{places.get(interval.location, interval.location)} · {interval.component}",
                "observation_type": observation_type,
                "failure_indicator": int(interval.end_type == std.FAILURE_END),
                "is_right_censored": int(interval.end_type != std.FAILURE_END),
                "life_hours_for_weibull": interval.life_hours,
                "life_hours_raw_elapsed": interval.raw_hours,
                "excluded_weekend_hours": weekend,
                "excluded_schedule_non_run_hours": non_run,
                "start_datetime": self._iso(interval.start),
                "end_datetime": None if interval.end_record_id is None else self._iso(interval.end),
                "analysis_cutoff_datetime": self._iso(cutoff),
                "start_task_id": starter["task_id"] if starter else None,
                "start_mapped_record_id": interval.start_record_id,
                "source_task_id": source["task_id"] if source else None,
                "source_mapped_record_id": interval.end_record_id,
                "source_work_title": source["task_name"] if source else None,
                "source_downtime_hours": source["downtime_hours"] if source else None,
                "source_request_description": source["requestor_description"] if source else None,
                "source_completion_notes": source["completion_notes"] if source else None,
                **{f"source_{key}": (source[key] if source else None) for key in NARRATIVE_KEYS},
                "weibull_life_note": interval.note,
                "data_quality_assumption_flag": (
                    self.life._duplicate_check_flag(interval.raw_hours) if interval.end_record_id is not None else None
                ),
            }
            rows.append(row)
        return rows

    def _events(
        self, analysis: std.RenewalAnalysis, by_id: Mapping[int, Mapping[str, Any]], places: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        """Every event the pooled streams saw and what it did there: the audit trail behind the lives."""

        rows = []
        for sequence, note in enumerate(
            sorted(analysis.notes, key=lambda note: ((note.location, note.component), note.when is None, note.when or datetime.min.replace(tzinfo=timezone.utc))),
            start=1,
        ):
            record = by_id.get(note.record_id) or {}
            rows.append({
                "sequence": sequence,
                "location": note.location,
                "component": note.component,
                "stream": f"{places.get(note.location, note.location)} · {note.component}",
                "record_id": note.record_id,
                "mapped_record_id": note.record_id,
                "task_id": record.get("task_id", note.task_id),
                "task_name": record.get("task_name"),
                "category": record.get("category"),
                "when": self._iso(note.when),
                "role": note.role,
                "note": note.note,
            })
        return rows

    def _life_basis(
        self, schedules: Mapping[str, Mapping[str, Any]], zone_name: str, zone_warning: str | None
    ) -> dict[str, Any]:
        """The schedule the lives were counted on, as the result view names it.

        One schedule for every pooled location reads as that schedule; locations on
        different schedules each count on their own, and the result says so.
        """

        distinct = {(s["schedule_code"], s["schedule_name"], s["hours_per_day"], s["exclude_weekends"]) for s in schedules.values()}
        if len(distinct) == 1:
            code, name, hours, weekends = next(iter(distinct))
        else:
            code, name, hours, weekends = None, "each location's own Weibull schedule", None, None
        return {
            "schedule_code": code,
            "schedule_name": name,
            "hours_per_day": hours,
            "exclude_weekends": weekends,
            "time_zone": zone_name,
            "time_zone_warning": zone_warning,
            "schedules": {location: dict(schedule) for location, schedule in sorted(schedules.items())},
        }

    def run_weibull(
        self,
        selection: Mapping[str, Any],
        *,
        analysis_start: date | None = None,
        analysis_cutoff: date | None = None,
        save: bool = False,
        run_by: str | None = None,
    ) -> dict[str, Any]:
        """Build a population's lives stream by stream, fit them, and (optionally) save the run.

        Reads every FAILURE and PM_RESET in reach; the population's location and
        component choose the streams that are pooled, and its mechanism chooses
        which failures count (``build_renewal_intervals``). When a failure with an
        unknown mechanism could be the selected one, both readings are fitted. The
        pattern views' categories, modes, causes and actions do not apply here: the
        run is saved without them, and a warning says when any were ticked.

        The result carries the fit flattened the way the result view reads it
        (``beta_mle``, ``observations``, ``life_basis`` and the rest) as well as the
        renewal-stream detail: the population definition, the streams, the
        warnings and the sensitivity fit.
        """

        asked = std.Population.from_mapping(selection)
        population = asked.for_life_analysis()
        connection = self.life.write_connection() if save else self.life.read_transaction()
        with connection as conn:
            loaded = self._load(conn, population)
            codebook, zone = loaded["codebook"], loaded["zone"]
            records = loaded["records"]
            events = []
            uncoded = unreviewed = 0
            for record in records:
                if record["category"] == std.NOT_REVIEWED:
                    unreviewed += 1
                    continue
                if record["category"] not in std.RENEWING_CATEGORIES:
                    continue
                event = self._stream_event(record, codebook)
                if not std.stream_touches(event):
                    uncoded += 1
                    continue
                events.append(event)
            start, cutoff, cutoff_source = self._resolve_window(conn, events, zone, analysis_start, analysis_cutoff)
            schedules = self._stream_schedules(conn, records, {event.location for event in events}, loaded["tree"])
            places = loaded["places"]

            def life_hours(stream: std.StreamKey, begin: datetime, end: datetime) -> float:
                schedule = schedules[stream[0]]
                return self.life._scheduled_life_hours(
                    begin, end, schedule["hours_per_day"], exclude_weekends=schedule["exclude_weekends"], tz=zone
                )[0]

            main = std.build_renewal_intervals(
                events, population, start=start, cutoff=cutoff, life_hours=life_hours,
                unknown_mechanism_as=std.UNKNOWN_AS_SUSPENSION,
            )
            sensitivity = None
            if main.unknown_decided:
                sensitivity = std.build_renewal_intervals(
                    events, population, start=start, cutoff=cutoff, life_hours=life_hours,
                    unknown_mechanism_as=std.UNKNOWN_AS_FAILURE,
                )
            window_start = (
                analysis_start
                if analysis_start is not None
                else min((event.event_date.astimezone(zone).date() for event in events if event.event_date), default=None)
            )
            window_end = analysis_cutoff if analysis_cutoff is not None else cutoff.astimezone(zone).date()
            definition = std.population_definition(population, window_start, window_end)
            by_id = {record["mapped_record_id"]: record for record in records}
            warnings = std.validity_warnings(
                main, asked, sensitivity=sensitivity, codebook=codebook,
                uncoded_count=uncoded, unreviewed_count=unreviewed,
            )
            if loaded["zone_warning"]:
                warnings.append({"code": "TIME_ZONE", "level": "info", "title": "Days split in UTC", "message": loaded["zone_warning"]})
            auto = sorted({
                event.record_id for event in main.stream_events
                if (by_id.get(event.record_id) or {}).get("disposition", {}).get("source") == "AUTO"
            })
            if auto:
                warnings.append({
                    "code": "AUTO_DISPOSITIONS",
                    "level": "warning",
                    "title": "Unconfirmed auto-dispositions",
                    "message": (
                        f"{len(auto)} of the work orders this fit reads {'was' if len(auto) == 1 else 'were'} "
                        "dispositioned by the disposition helper and not yet confirmed by an engineer. Confirm them "
                        "on the Disposition page (Show: Auto-dispositioned) before relying on this fit."
                    ),
                })
            stream_schedules = {location: schedules[location] for location, _ in main.streams}
            stream_rows = []
            for location, component in main.streams:
                stream_intervals = [i for i in main.intervals if i.stream == (location, component)]
                stream_rows.append({
                    "location": location,
                    "location_label": places.get(location, location),
                    "component": component,
                    "component_name": codebook.name(std.COMPONENT, component),
                    "failures": sum(1 for i in stream_intervals if i.end_type == std.FAILURE_END),
                    "suspensions": sum(1 for i in stream_intervals if i.end_type == std.SUSPENSION_END),
                    "recurrent": (location, component) in main.recurrent_streams,
                    "schedule_name": schedules[location]["schedule_name"],
                })
            fit = self._fit(main, "Unknown mechanisms as suspensions" if sensitivity else "Fit")
            observations = self._observations(main, by_id, schedules, zone, cutoff, places)
            payload: dict[str, Any] = {
                "analysis_id": None,
                "result_id": None,
                "analysis_label": self._population_label(population, loaded["tree"]),
                "population_definition": definition,
                "selection": population.to_mapping(),
                "method_version": RENEWAL_METHOD_VERSION,
                "software_version": self._software_version(),
                "analysis_start": self._iso(start),
                "analysis_cutoff": self._iso(cutoff),
                "analysis_cutoff_source": cutoff_source,
                "window_start_day": window_start.isoformat() if window_start else None,
                "window_end_day": window_end.isoformat(),
                "life_basis": self._life_basis(stream_schedules, loaded["zone_name"], loaded["zone_warning"]),
                "streams": stream_rows,
                "fit": fit,
                "sensitivity_fit": self._fit(sensitivity, "Unknown mechanisms as failures"),
                "warnings": warnings,
                "observations": observations,
                "events": self._events(main, by_id, places),
                "sensitivity_observations": (
                    self._observations(sensitivity, by_id, schedules, zone, cutoff, places) if sensitivity else []
                ),
                "total_observation_count": len(observations),
                "failure_count": sum(1 for row in observations if row["failure_indicator"]),
                "censored_count": sum(1 for row in observations if not row["failure_indicator"]),
                "suspension_count": sum(1 for row in observations if row["observation_type"] == "SUSPENDED_LIFE"),
                "current_life_censored_count": sum(1 for row in observations if row["observation_type"] == "RIGHT_CENSORED_LIFE"),
                "run_by": run_by,
                "run_at": None,
            }
            payload.update(self._flat_fit(fit))
            if save:
                analysis_id = conn.execute(
                    """
                    INSERT INTO population_weibull_analysis(population_definition, selection_json, window_start,
                        window_end, cutoff_source, method_version, software_version, failure_count, suspension_count,
                        beta, eta, result_json, run_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
                    """,
                    (
                        definition,
                        json.dumps(payload["selection"]),
                        payload["analysis_start"],
                        payload["analysis_cutoff"],
                        cutoff_source,
                        RENEWAL_METHOD_VERSION,
                        payload["software_version"],
                        fit["failure_count"],
                        fit["suspension_count"],
                        fit.get("beta"),
                        fit.get("eta"),
                        run_by,
                    ),
                ).lastrowid
                payload["analysis_id"] = payload["result_id"] = int(analysis_id)
                payload["run_at"] = conn.execute(
                    "SELECT run_at FROM population_weibull_analysis WHERE population_weibull_analysis_id = ?",
                    (analysis_id,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE population_weibull_analysis SET result_json = ? WHERE population_weibull_analysis_id = ?",
                    (json.dumps(payload), analysis_id),
                )
            payload.update(self._currency(conn, payload, loaded["tree"]))
        payload["saved"] = bool(save)
        return payload

    @staticmethod
    def _flat_fit(fit: Mapping[str, Any]) -> dict[str, Any]:
        """The fit under the names the result view and the report read."""

        return {
            "fitted": bool(fit.get("fitted")),
            "fit_reason": fit.get("reason"),
            "meets_minimum": bool(fit.get("fitted")),
            "min_failure_lives": fit.get("min_failure_lives"),
            "beta_mle": fit.get("beta"),
            "eta_mle": fit.get("eta"),
            "beta_lower_ci": fit.get("beta_lower_ci"),
            "beta_upper_ci": fit.get("beta_upper_ci"),
            "eta_lower_ci": fit.get("eta_lower_ci"),
            "eta_upper_ci": fit.get("eta_upper_ci"),
            "mean_time_to_failure": fit.get("mean_time_to_failure"),
            "b10_life": fit.get("b10_life"),
            "b50_life": fit.get("b50_life"),
            "probability_plot_r_squared": fit.get("probability_plot_r_squared"),
            "probability_plot_r_squared_threshold": fit.get("probability_plot_r_squared_threshold"),
            "probability_plot_review": bool(fit.get("probability_plot_review")),
            "km_points": fit.get("km_points") or [],
            "curve_points": fit.get("curve_points") or [],
            "interpretation_summary": fit.get("interpretation_summary") or [],
        }

    def _currency(
        self, conn: sqlite3.Connection, payload: Mapping[str, Any], tree: AssetTree | None = None
    ) -> dict[str, Any]:
        """Whether a run still reads as today's would: method, schedules and plant time zone."""

        basis = payload.get("life_basis") or {}
        counted_on = basis.get("schedules") or {}
        tree = tree or self.records.asset_tree(conn)
        current = self._stream_schedules(conn, [], counted_on, tree)
        changed = sorted(
            location for location, schedule in counted_on.items()
            if current[location]["schedule_code"] != schedule.get("schedule_code")
        )
        zone_name = self.life._plant_time_zone(conn)[1]
        return {
            "method_current": payload.get("method_version") == RENEWAL_METHOD_VERSION,
            "schedule_current": not changed,
            "schedules_changed": [
                {
                    "location": location,
                    "location_label": tree.label(location),
                    "counted_on": counted_on[location].get("schedule_name"),
                    "now_on": current[location]["schedule_name"],
                }
                for location in changed
            ],
            "current_schedule_name": ", ".join(current[location]["schedule_name"] for location in changed) or None,
            "time_zone_current": (basis.get("time_zone") or "UTC") == zone_name,
            "current_time_zone": zone_name,
        }

    @staticmethod
    def _software_version() -> str:
        from services.life_data_service import gremlin_code_version

        return gremlin_code_version()

    # ------------------------------------------------------------------ #
    # Saved runs
    # ------------------------------------------------------------------ #
    def _saved_payload(
        self, conn: sqlite3.Connection, analysis_id: int, *, token: Any = None, tree: AssetTree | None = None
    ) -> dict[str, Any] | None:
        """A saved run as the page shows it. ``tree`` is the asset tree when the caller
        has it already; else ``token`` is GREMLIN.db's change token, read before
        ``conn``'s transaction first queried, when the caller has one."""

        row = conn.execute(
            "SELECT result_json FROM population_weibull_analysis WHERE population_weibull_analysis_id = ?",
            (int(analysis_id),),
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["result_json"] or "{}")
        payload["saved"] = True
        payload.update(self._currency(conn, payload, tree or self.records.asset_tree(conn, token=token)))
        adjustment = conn.execute(
            """
            SELECT adjusted_beta, adjusted_eta, adjustment_reason, adjusted_by, adjusted_at
            FROM population_weibull_adjustment
            WHERE population_weibull_analysis_id = ? AND is_current = 1
            ORDER BY population_weibull_adjustment_id DESC LIMIT 1
            """,
            (int(analysis_id),),
        ).fetchone()
        payload["adjustment"] = dict(adjustment) if adjustment else None
        return payload

    def load_saved(self, analysis_id: int) -> dict[str, Any] | None:
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            return self._saved_payload(conn, analysis_id, token=token)

    def latest_saved(self, selection: Mapping[str, Any]) -> dict[str, Any] | None:
        """The newest saved run of exactly this population, whatever its window."""

        key = _life_selection_key(selection)
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            for row in conn.execute(
                "SELECT population_weibull_analysis_id, selection_json FROM population_weibull_analysis "
                "ORDER BY run_at DESC, population_weibull_analysis_id DESC"
            ):
                try:
                    saved_key = _selection_key(json.loads(row["selection_json"] or "{}"))
                except ValueError:
                    continue
                if saved_key == key:
                    return self._saved_payload(conn, int(row["population_weibull_analysis_id"]), token=token)
        return None

    def saved_analyses(self, *, asset_number: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """The most recent saved runs, newest first; only those that read ``asset_number`` when given."""

        with self.life.connect() as conn:
            rows = conn.execute(
                """
                SELECT population_weibull_analysis_id, population_definition, selection_json, method_version,
                       failure_count, suspension_count, beta, eta, run_by, run_at
                FROM population_weibull_analysis
                ORDER BY run_at DESC, population_weibull_analysis_id DESC
                """
            ).fetchall()
        saved = []
        for row in rows:
            try:
                selection = json.loads(row["selection_json"] or "{}")
            except ValueError:
                selection = {}
            if asset_number and asset_number not in (selection.get("assets") or []) and asset_number not in (
                selection.get("locations") or []
            ):
                continue
            saved.append({
                "analysis_id": int(row["population_weibull_analysis_id"]),
                "population_definition": row["population_definition"],
                "selection": selection,
                "method_current": row["method_version"] == RENEWAL_METHOD_VERSION,
                "failure_count": row["failure_count"],
                "suspension_count": row["suspension_count"],
                "beta": row["beta"],
                "eta": row["eta"],
                "run_by": row["run_by"],
                "run_at": row["run_at"],
            })
            if len(saved) >= limit:
                break
        return saved

    def save_adjustment(
        self, analysis_id: int, beta: Any, eta: Any, reason: Any = "", *, adjusted_by: str | None = None
    ) -> dict[str, Any]:
        """Keep an editor's beta and eta beside a saved run's MLE, never over it."""

        try:
            beta_value, eta_value = float(beta), float(eta)
        except (TypeError, ValueError):
            raise ValueError("Adjusted beta and eta must both be positive numbers.") from None
        if not (math.isfinite(beta_value) and math.isfinite(eta_value) and beta_value > 0 and eta_value > 0):
            raise ValueError("Adjusted beta and eta must both be positive numbers.")
        text = std.clean_text(reason)
        if text is None:
            raise ValueError("Say why beta and eta were adjusted: the reason is kept with them.")
        with self.life.write_connection() as conn:
            row = conn.execute(
                "SELECT beta FROM population_weibull_analysis WHERE population_weibull_analysis_id = ?", (int(analysis_id),)
            ).fetchone()
            if row is None:
                raise LookupError("That Weibull analysis was not found.")
            if row["beta"] is None:
                raise ValueError("This run has no fit to adjust.")
            conn.execute(
                "UPDATE population_weibull_adjustment SET is_current = 0 WHERE population_weibull_analysis_id = ?",
                (int(analysis_id),),
            )
            conn.execute(
                """
                INSERT INTO population_weibull_adjustment(population_weibull_analysis_id, adjusted_beta, adjusted_eta,
                    adjustment_reason, adjusted_by)
                VALUES (?, ?, ?, ?, ?)
                """,
                (int(analysis_id), beta_value, eta_value, text, adjusted_by),
            )
            current = conn.execute(
                """
                SELECT adjusted_beta, adjusted_eta, adjustment_reason, adjusted_by, adjusted_at
                FROM population_weibull_adjustment
                WHERE population_weibull_analysis_id = ? AND is_current = 1
                """,
                (int(analysis_id),),
            ).fetchone()
        return dict(current)

    # ------------------------------------------------------------------ #
    # Rankings of the saved fits
    # ------------------------------------------------------------------ #
    def _ranked_fits(
        self,
        conn: sqlite3.Connection,
        population: std.Population,
        tree: AssetTree | None = None,
        *,
        token: Any = None,
    ) -> list[dict[str, Any]]:
        """The latest saved fit of each single-component population the page's population reaches.

        Only fits resting on at least the minimum number of failure lives, and only
        populations of one component: a beta pooled over different items blurs
        their patterns into one. Each carries what both rankings need -- the fit,
        its R², each stream's current life at the run's cutoff and the schedule it
        was counted on -- and whether it is still current.

        Reading them means reading every saved run's whole result, so they are kept,
        as a population's records are, until GREMLIN.db changes: ``token`` is its
        change token, read before ``conn``'s transaction first queried. Nothing that
        reads the list changes it.
        """

        from services.life_data_service import MIN_WEIBULL_FAILURE_LIVES

        tree = tree or self.records.asset_tree(conn)
        reach = set(population.assets) | set(population.locations)
        for asset in population.assets:
            reach.update(option["asset_number"] for option in tree.descendants(asset))
        key = (token, tree.serial, tuple(sorted(reach))) if token is not None else None
        if key is not None:
            with _LOADED_LOCK:
                cached = _RANKED.get(key)
                if cached is not None:
                    _RANKED.move_to_end(key)
                    return cached
        seen: set[str] = set()
        fits = []
        for row in conn.execute(
            "SELECT population_weibull_analysis_id, selection_json, failure_count, beta FROM population_weibull_analysis "
            "ORDER BY run_at DESC, population_weibull_analysis_id DESC"
        ):
            try:
                selection = json.loads(row["selection_json"] or "{}")
                key = _selection_key(selection)
            except ValueError:
                continue
            if key in seen:
                continue
            seen.add(key)
            if std.Population.from_mapping(selection).narrows_patterns_only():
                # Saved before the Weibull stopped reading mode, cause and action.
                continue
            if row["beta"] is None or int(row["failure_count"] or 0) < MIN_WEIBULL_FAILURE_LIVES:
                continue
            if len(selection.get("components") or []) != 1:
                continue
            if reach and not (set(selection.get("assets") or []) | set(selection.get("locations") or [])) & reach:
                continue
            payload = self._saved_payload(conn, int(row["population_weibull_analysis_id"]), tree=tree)
            if not payload or not payload.get("fitted"):
                continue
            schedules = (payload.get("life_basis") or {}).get("schedules") or {}
            current_lives = [
                {
                    "location": obs["location"],
                    "life_hours": float(obs["life_hours_for_weibull"]),
                    "weekly_hours": self.life._weekly_schedule_hours(
                        schedules.get(obs["location"], {}).get("hours_per_day"),
                        schedules.get(obs["location"], {}).get("exclude_weekends"),
                    ),
                }
                for obs in payload.get("observations") or []
                if obs.get("observation_type") == "RIGHT_CENSORED_LIFE"
            ]
            fits.append({
                "analysis_id": payload["analysis_id"],
                "label": payload.get("analysis_label"),
                "population_definition": payload.get("population_definition"),
                "selection": payload.get("selection"),
                "beta_mle": float(payload["beta_mle"]),
                "eta_mle": float(payload["eta_mle"]),
                "failure_count": int(payload["failure_count"]),
                "probability_plot_r_squared": payload.get("probability_plot_r_squared"),
                "probability_plot_review": bool(payload.get("probability_plot_review")),
                "analysis_cutoff": payload.get("analysis_cutoff"),
                "analysis_cutoff_source": payload.get("analysis_cutoff_source"),
                "time_zone": (payload.get("life_basis") or {}).get("time_zone") or "UTC",
                "run_at": payload.get("run_at"),
                "current_lives": current_lives,
                "method_current": payload["method_current"],
                "schedule_current": payload["schedule_current"],
                "time_zone_current": payload["time_zone_current"],
            })
        if key is not None:
            with _LOADED_LOCK:
                _RANKED[key] = fits
                while len(_RANKED) > _LOADED_SIZE:
                    _RANKED.popitem(last=False)
        return fits

    @staticmethod
    def _beta_rankings(fits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Where an age-based PM is most likely to pay off: the strongest wear-out patterns first."""

        ranked = sorted(fits, key=lambda fit: (-fit["beta_mle"], -fit["failure_count"], str(fit["label"])))
        return [{key: value for key, value in fit.items() if key != "current_lives"} for fit in ranked[:RANKING_LIMIT]]

    @staticmethod
    def _risk_rankings(fits: list[dict[str, Any]], weeks: float) -> list[dict[str, Any]]:
        """The saved populations most likely to see a failure in the next ``weeks`` weeks.

        For each item still in service at the run's cutoff, the chance its current
        life ends within the window given it has lasted this long,
        ``1 - R(t + Δ) / R(t)``, with Δ the weeks in that location's scheduled
        hours. A population pools several items, so its chance is that of any of
        them failing: one minus the chance all of them survive.
        """

        try:
            weeks = float(weeks)
        except (TypeError, ValueError):
            raise ValueError("The window has to be between 1 and 52 weeks.") from None
        if not (math.isfinite(weeks) and 1 <= weeks <= 52):
            raise ValueError("The window has to be between 1 and 52 weeks.")
        ranked = []
        for fit in fits:
            if not fit["current_lives"]:
                continue
            beta, eta = fit["beta_mle"], fit["eta_mle"]
            survive = 1.0
            for life in fit["current_lives"]:
                t, window = life["life_hours"], weeks * life["weekly_hours"]
                try:
                    survive *= math.exp((t / eta) ** beta - ((t + window) / eta) ** beta)
                except (OverflowError, ZeroDivisionError, ValueError):
                    survive = 0.0
            ranked.append({
                **{key: value for key, value in fit.items() if key != "current_lives"},
                "window_weeks": weeks,
                "items_in_service": len(fit["current_lives"]),
                "current_life_hours": max(life["life_hours"] for life in fit["current_lives"]),
                "probability": 1.0 - survive,
            })
        ranked.sort(key=lambda fit: (-fit["probability"], -fit["failure_count"], str(fit["label"])))
        return ranked[:RANKING_LIMIT]

    def risk_rankings(self, selection: Mapping[str, Any], *, weeks: float = 4) -> list[dict[str, Any]]:
        population = std.Population.from_mapping(selection)
        token = self.life.change_token()
        with self.life.read_transaction() as conn:
            fits = self._ranked_fits(conn, population, self.records.asset_tree(conn, token=token), token=token)
        return self._risk_rankings(fits, weeks)

    # ------------------------------------------------------------------ #
    # Report
    # ------------------------------------------------------------------ #
    def build_report(self, analysis_id: Any, payload: Mapping[str, Any], output_path: "str | Path") -> str:
        """Write a numbered Weibull report for a saved run and return its report number.

        Everything it states is read back from the saved run, never from the
        request; only the chart images and the age to evaluate come from the
        client. A run saved under an earlier method, counted on a schedule or in a
        time zone that has since changed, or resting on too few failure lives is
        refused before a number is reserved.
        """

        from services.life_data_service import MIN_WEIBULL_FAILURE_LIVES

        try:
            analysis_id = int(analysis_id)
        except (TypeError, ValueError):
            raise ValueError("A saved Weibull analysis is needed to generate a report.") from None
        result = self.load_saved(analysis_id)
        if result is None:
            raise LookupError("That Weibull analysis no longer exists. Run it again before generating a report.")
        if not result["method_current"]:
            raise ValueError(
                f"This analysis was saved by an earlier version of the method ({result.get('method_version') or 'unrecorded'}). "
                "Run it again before reporting it."
            )
        if not result["schedule_current"]:
            raise ValueError(
                "The life hours were counted on a schedule some of its locations are no longer on. Run it again before "
                "reporting it."
            )
        if not result["time_zone_current"]:
            raise ValueError(
                f"Its days were split in {(result.get('life_basis') or {}).get('time_zone') or 'UTC'}, but the plant's time "
                f"zone is now {result['current_time_zone']}. Run it again before reporting it."
            )
        if not result.get("fitted"):
            raise ValueError(
                f"This analysis rests on {result.get('failure_count', 0)} lives that end in a failure; a Weibull report "
                f"needs at least {MIN_WEIBULL_FAILURE_LIVES}."
            )
        target_age_hours = None
        raw_age = payload.get("target_age_hours")
        if raw_age not in (None, ""):
            try:
                target_age_hours = float(raw_age)
            except (TypeError, ValueError):
                target_age_hours = None
            if target_age_hours is None or not math.isfinite(target_age_hours) or target_age_hours <= 0:
                raise ValueError("The age to evaluate reliability at has to be a positive number of hours.")
        charts = []
        for chart in payload.get("charts") or []:
            if not isinstance(chart, Mapping):
                continue
            png = self.life._decode_data_url_png(chart.get("image"))
            if png:
                charts.append({"title": chart.get("title"), "_png_bytes": png})
        selection = result.get("selection") or {}
        assets = selection.get("assets") or []
        report_asset = assets[0] if len(assets) == 1 else (selection.get("locations") or ["POP"])[0]
        report_number, _ = self.life.next_weibull_report_number(
            report_asset, analysis_label=result.get("analysis_label") or "", population_weibull_analysis_id=analysis_id,
        )
        body = self.life._weibull_report_body(
            report_number,
            ", ".join(assets) or report_asset,
            result.get("analysis_label") or "Selected population",
            result,
            charts,
            target_age_hours=target_age_hours,
            population=result,
        )
        self.life._write_docx(output_path, body, charts)
        return report_number

    # ------------------------------------------------------------------ #
    # Show me around
    # ------------------------------------------------------------------ #
    def tour_example(self) -> dict[str, Any] | None:
        """What the walk-through shows somebody who hasn't picked anything.

        A population with a saved fit first, since opening a saved fit is the only
        Weibull the tour can show without running one of its own; otherwise the
        asset with the most coded failures, which fill the Pareto and every chart.
        """

        with self.life.read_transaction() as conn:
            for row in conn.execute(
                "SELECT selection_json FROM population_weibull_analysis WHERE beta IS NOT NULL "
                "ORDER BY run_at DESC, population_weibull_analysis_id DESC"
            ):
                try:
                    selection = std.Population.from_mapping(json.loads(row["selection_json"] or "{}")).to_mapping()
                except ValueError:
                    continue
                if len(selection["assets"]) == 1:
                    return {"asset": selection["assets"][0], "selection": selection, "saved_fit": True}
            row = conn.execute(
                """
                SELECT TRIM(m.asset_number) AS asset_number, COUNT(*) AS failures
                FROM wo_disposition w
                JOIN mapped_cmms_record m ON m.mapped_record_id = w.mapped_record_id
                WHERE w.is_current = 1 AND w.disposition_category = 'FAILURE' AND w.component_code IS NOT NULL
                  AND TRIM(COALESCE(m.asset_number, '')) <> ''
                GROUP BY TRIM(m.asset_number)
                ORDER BY failures DESC, asset_number
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {"asset": row["asset_number"], "selection": {"assets": [row["asset_number"]]}, "saved_fit": False}
