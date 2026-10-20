"""An asset's sub-assets: by the plant's numbering, then Limble's parentAssetID.

The plant numbers a sub-asset by its parent's number and a suffix: 4002-S09 is
a sub-asset of 4002. Work orders are filed under those curated Asset Numbers,
while Limble numbers the same assets by its own ids (13042, 4109), so a
hierarchy read only by Limble id loses every sub-asset whose work orders it
cannot tie back to an id. And Limble sends a ``parentID`` on top-level assets
that is not their parent: work orders imported while the sync still fell back
to it name another top-level machine as the parent. So the tree reads an
asset's number first, then Limble's ``parentAssetID`` (the sync's stored asset
list, then the PM calendar's), and the work orders' own parent last.
"""

import importlib
import tempfile
import unittest
from pathlib import Path

from repositories.raw_repo import RawRepository
from services.ingestion_service import AssetIndex, IngestionService, _asset_parent_id
from services.life_data_service import LifeDataService
from services.population_analysis_service import PopulationAnalysisService
from services.standard_disposition_schema import AssetTree
from services.standard_disposition_service import StandardDispositionService

WATER, PFS, COMPRESSOR, BOILER, SANDER, OVEN, BURNER = "900", "13042", "6274", "7000", "4101", "4109", "4201"
NAMES = {
    WATER: "4002-S18 Deionized Water System",
    PFS: "4002 Panel Finishing System",
    COMPRESSOR: "6274 Sullair Air Compressor",
    BOILER: "7000 Boiler",
    SANDER: "4002-S01 Sander",
    OVEN: "4002-S09 Dry-off oven",
    BURNER: "4002-S09 Burner",
}
# What the PM calendar stores from /assets: parentAssetID only.
PM_HIERARCHY = [
    {"asset_id": WATER, "asset_name": NAMES[WATER], "parent_asset_id": None},
    {"asset_id": PFS, "asset_name": NAMES[PFS], "parent_asset_id": None},
    {"asset_id": COMPRESSOR, "asset_name": NAMES[COMPRESSOR], "parent_asset_id": None},
    {"asset_id": BOILER, "asset_name": NAMES[BOILER], "parent_asset_id": None},
    {"asset_id": SANDER, "asset_name": NAMES[SANDER], "parent_asset_id": PFS},
    {"asset_id": OVEN, "asset_name": NAMES[OVEN], "parent_asset_id": PFS},
    {"asset_id": BURNER, "asset_name": NAMES[BURNER], "parent_asset_id": OVEN},
]


def seed(life):
    """Work orders as the old sync stored them: top-level assets name a wrong parent."""

    with life.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        rows = [
            (1, PFS, WATER),        # parentID read as a parent: wrong
            (2, PFS, WATER),
            (3, COMPRESSOR, WATER),  # wrong too
            (4, BOILER, PFS),        # a boiler under the paint line: wrong
            (5, SANDER, PFS),        # parentAssetID: right
        ]
        for record_id, asset, parent in rows:
            conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (record_id,))
            conn.execute(
                """
                INSERT INTO mapped_cmms_record (mapped_record_id, raw_record_id, import_batch_id, asset_number,
                    asset_name, asset_id_raw, immediate_parent_asset_id, task_id, task_name, created_date_final,
                    completed_date_final, record_class_auto, mapping_version)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'Work', '2024-01-01', '2024-01-01', 'CORRECTIVE_WO', 'v4')
                """,
                (record_id, record_id, asset, NAMES[asset], asset, parent, str(record_id)),
            )


class AssetTreeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.life = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        seed(self.life)

    def numbers(self, tree, asset):
        return [(option["asset_number"], option["depth"]) for option in tree.descendants(asset)]

    def test_the_work_orders_alone_carry_the_wrong_parents(self):
        with self.life.connect() as conn:
            tree = AssetTree.load(conn)
        # With nothing else to go on, the boiler's work order still names the
        # paint line, wrongly. A parent no work order or Limble row names as an
        # asset (the water system's id, read from parentID) is not believed.
        self.assertEqual(self.numbers(tree, PFS), [(SANDER, 1), (BOILER, 1)])
        self.assertEqual(self.numbers(tree, WATER), [])
        self.assertIsNone(tree.parent_of(PFS))

    def test_the_pm_calendars_hierarchy_wins_wherever_it_knows_the_asset(self):
        with self.life.connect() as conn:
            tree = AssetTree.load(conn, PM_HIERARCHY)
        # Every level down, in name order; the oven has no work orders of its own
        # yet and is still offered, with the burner under it. The boiler and the
        # compressor are top-level in Limble, which outranks their work orders;
        # the water system is too, but it is numbered 4002-S18.
        self.assertEqual(self.numbers(tree, PFS), [(SANDER, 1), (OVEN, 1), (BURNER, 2), (WATER, 1)])
        self.assertEqual(self.numbers(tree, WATER), [])
        self.assertEqual(tree.label(OVEN), "4002-S09 Dry-off oven (4109)")
        self.assertEqual(tree.ancestors(BURNER), [OVEN, PFS])

    def test_an_asset_the_calendar_does_not_know_keeps_its_work_orders_parent(self):
        with self.life.connect() as conn:
            tree = AssetTree.load(conn, [row for row in PM_HIERARCHY if row["asset_id"] != BOILER])
        self.assertIn(BOILER, [number for number, _ in self.numbers(tree, PFS)])

    def test_the_editor_and_the_analysis_read_the_corrected_tree(self):
        records = StandardDispositionService(self.life, asset_hierarchy=lambda: PM_HIERARCHY)
        offered = [option["asset_number"] for option in records.editor_options(PFS)["sub_assets"]]
        self.assertEqual(offered, [SANDER, OVEN, BURNER, WATER])
        # The boiler's work order is not in reach of the paint line any more.
        analysis = PopulationAnalysisService(self.life, records)
        downtime = analysis.downtime({"assets": [PFS], "categories": ["NOT_REVIEWED"]})
        self.assertEqual(downtime["summary"]["work_order_count"], 3)
        options = analysis.options([PFS])
        self.assertEqual([option["asset_number"] for option in options["sub_assets"]], [SANDER, OVEN, BURNER, WATER])

    def test_a_broken_calendar_falls_back_to_the_work_orders(self):
        def unreadable():
            raise OSError("share is down")

        records = StandardDispositionService(self.life, asset_hierarchy=unreadable)
        self.assertEqual([option["asset_number"] for option in records.editor_options(PFS)["sub_assets"]], [SANDER, BOILER])

    def test_a_legacy_mechanism_names_a_sub_asset_by_its_name(self):
        from services import disposition_standard as std

        with self.life.connect() as conn:
            tree = AssetTree.load(conn, PM_HIERARCHY)
        converted = std.legacy_auto_conversion(
            "S09 clogged air filter",
            asset_number=PFS,
            sub_asset_choices=[option["asset_number"] for option in tree.descendants(PFS)],
            codebook=std.CodeBook.from_seed(),
            asset_names=tree.names,
        )
        # The oven and its burner both start "4002-S09"; only the oven's name is
        # one word that ends in S09 and nothing else, so the burner is a second
        # match and the conversion is refused rather than guessed.
        self.assertIsNone(converted)
        converted = std.legacy_auto_conversion(
            "S01 clogged air filter",
            asset_number=PFS,
            sub_asset_choices=[option["asset_number"] for option in tree.descendants(PFS)],
            codebook=std.CodeBook.from_seed(),
            asset_names=tree.names,
        )
        self.assertEqual(converted["sub_asset"], SANDER)


def seed_curated(life):
    """Work orders as the plant files them: under curated Asset Numbers, not Limble ids.

    4002's own work orders carry its Limble id; 4002-S09's carry its id and a
    parent; 4002-S01's carry neither (rows from before the sync), 4002-S05's
    name a parent no row ties to 4002. 40021 and 3101-3107 are other machines
    whose numbers only look alike.
    """

    with life.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")
        rows = [
            (1, "4002", "4002 Panel Finishing System", PFS, None),
            (2, "4002", "4002 Panel Finishing System", PFS, "900"),  # parentID read as a parent
            (3, "4002-S09", "4002-S09 Dry-off oven", OVEN, PFS),
            (4, "4002-S01", "4002-S01 Sander", None, None),
            (5, "4002-S05", "4002-S05 Activator", None, "99999"),
            (6, "40021", "40021 Wash line", "40021", None),
            (7, "3101-3107", "3101-3107 Salvagnini", "9000", None),
            (8, "3101", "3101 Salvagnini Bender", "3101", None),
            (9, "13042", "4002 Panel Finishing System", PFS, None),  # a new task the sync could not bridge
        ]
        for record_id, number, name, asset_id, parent in rows:
            conn.execute("INSERT INTO raw_cmms_record (raw_record_id, raw_json) VALUES (?, '{}')", (record_id,))
            conn.execute(
                """
                INSERT INTO mapped_cmms_record (mapped_record_id, raw_record_id, import_batch_id, asset_number,
                    asset_name, asset_id_raw, immediate_parent_asset_id, task_id, task_name, created_date_final,
                    completed_date_final, record_class_auto, mapping_version)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'Work', '2024-01-01', '2024-01-01', 'CORRECTIVE_WO', 'v4')
                """,
                (record_id, record_id, number, name, asset_id, parent, str(record_id)),
            )


# Limble's /assets, as the sync stores it: ids, names, parentAssetID.
LIMBLE_ASSETS = [
    {"asset_id": PFS, "asset_name": "4002 Panel Finishing System", "parent_asset_id": "706"},
    {"asset_id": "706", "asset_name": "Building 706", "parent_asset_id": None},
    {"asset_id": OVEN, "asset_name": "4002-S09 Dry-off oven", "parent_asset_id": PFS},
    {"asset_id": BURNER, "asset_name": "4002-S09 Burner", "parent_asset_id": OVEN},
    {"asset_id": SANDER, "asset_name": "4002-S01 Sander", "parent_asset_id": PFS},
    {"asset_id": "4111", "asset_name": "4002-S11 Riser bank", "parent_asset_id": PFS},
    {"asset_id": "4199", "asset_name": "Spray booth exhaust fan", "parent_asset_id": PFS},
    {"asset_id": "40021", "asset_name": "40021 Wash line", "parent_asset_id": "706"},
]


class CuratedNumberTests(unittest.TestCase):
    """The plant's own Asset Numbers, as the user sees them: 4002 and 4002-S01 to 4002-S18."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.life = LifeDataService(Path(tmp.name) / "gremlin.db", refresh_on_startup=False)
        seed_curated(self.life)

    def offered(self, asset="4002", hierarchy=None):
        records = StandardDispositionService(self.life, asset_hierarchy=lambda: hierarchy)
        return [(option["asset_number"], option["depth"]) for option in records.editor_options(asset)["sub_assets"]]

    def test_the_numbering_alone_finds_every_sub_asset(self):
        # No Limble list at all: 4002-S01, -S05 and -S09 by their numbers, and
        # nothing that only looks alike (40021, 3101 under 3101-3107).
        self.assertEqual(self.offered(), [("4002-S01", 1), ("4002-S05", 1), ("4002-S09", 1)])
        self.assertEqual(self.offered("3101-3107"), [])
        self.assertEqual(self.offered("40021"), [])

    def test_limble_adds_the_sub_assets_with_no_work_orders_and_those_numbered_otherwise(self):
        RawRepository(self.life.db_path).replace_limble_assets(LIMBLE_ASSETS)
        offered = self.offered()
        # The burner shares 4002-S09's code and hangs under it in Limble; the
        # riser bank and the fan have no work orders, so they go by their ids
        # (what the sync will file their first work order under), and the fan's
        # name has no number, so only Limble puts it here. The sander is the
        # same asset as 4002-S01 and is offered once.
        self.assertEqual(offered, [
            ("4002-S01", 1), ("4002-S05", 1), ("4002-S09", 1), (BURNER, 2), ("4111", 1), ("4199", 1),
        ])
        with self.life.connect() as conn:
            tree = AssetTree.load(conn)
        self.assertEqual(tree.label("4002-S09"), "4002-S09 Dry-off oven")
        self.assertEqual(tree.label("4111"), "4002-S11 Riser bank (4111)")
        # The building above the line is Limble's grouping, not a sub-asset of anything.
        self.assertEqual(tree.parent_of("4002"), "706")

    def test_a_limble_id_that_is_another_machines_asset_number_is_not_that_machine(self):
        # Limble's ids and the plant's numbers share a range: Limble asset 4002
        # is a forklift, and Limble asset 3101 is the bender filed under 3101.
        RawRepository(self.life.db_path).replace_limble_assets([
            *LIMBLE_ASSETS,
            {"asset_id": "4002", "asset_name": "Forklift 12", "parent_asset_id": "706"},
            {"asset_id": "4005", "asset_name": "4002-S05 Activator", "parent_asset_id": PFS},
            {"asset_id": "3101", "asset_name": "3101 Salvagnini Bender", "parent_asset_id": "9000"},
        ])
        with self.life.connect() as conn:
            tree = AssetTree.load(conn)
        self.assertEqual(tree.name("4002"), "4002 Panel Finishing System")
        self.assertEqual(tree.parent_of("4002"), "706")
        # The activator's Limble row is 4002-S05, whose work orders carry no id.
        self.assertEqual(tree.name("4002-S05"), "4002-S05 Activator")
        self.assertNotIn("4005", [option["asset_number"] for option in tree.descendants("4002")])

    def test_the_pm_calendar_stands_in_until_the_sync_has_stored_the_list(self):
        self.assertIn(("4111", 1), self.offered(hierarchy=LIMBLE_ASSETS))

    def test_the_analysis_reaches_the_sub_assets_work_orders(self):
        records = StandardDispositionService(self.life)
        analysis = PopulationAnalysisService(self.life, records)
        downtime = analysis.downtime({"assets": ["4002"], "categories": ["NOT_REVIEWED"]})
        # 4002's two, and one each on S01, S05 and S09. The one filed under the
        # bare id 13042 is 4002 itself, not a sub-asset of it.
        self.assertEqual(downtime["summary"]["work_order_count"], 5)
        with self.life.connect() as conn:
            tree = AssetTree.load(conn)
        self.assertEqual(tree.aliases, {PFS: "4002"})
        self.assertEqual([option["asset_number"] for option in tree.descendants(PFS)], ["4002-S01", "4002-S05", "4002-S09"])


class IngestionParentTests(unittest.TestCase):
    def test_only_parent_asset_id_is_read_as_the_parent(self):
        assets = [
            {"assetID": 900, "name": NAMES[WATER], "parentAssetID": 0, "parentID": 0},
            {"assetID": 13042, "name": NAMES[PFS], "parentAssetID": 0, "parentID": 900},
            {"assetID": 4101, "name": NAMES[SANDER], "parentAssetID": 13042, "parentID": 900},
        ]
        self.assertIsNone(_asset_parent_id(assets[1]))
        self.assertEqual(_asset_parent_id(assets[2]), "13042")
        index = AssetIndex.from_assets(assets)
        top, child = {}, {}
        index.enrich(top, 13042)
        index.enrich(child, 4101)
        self.assertNotIn("Immediate Parent Asset ID", top)
        self.assertEqual(top["Root Asset ID"], 13042)
        self.assertEqual(child["Immediate Parent Asset ID"], "13042")
        self.assertEqual(top["Asset Has Children"], 1)

    def test_a_sync_stores_the_asset_list_for_the_tree(self):
        class Client:
            def get_tasks(self, updated_since=None, on_page=None):
                return []

            def get_assets(self, on_page=None):
                return [
                    {"assetID": 13042.0, "name": "4002 Panel Finishing System", "parentAssetID": 0, "parentID": 900},
                    {"assetID": 4101, "name": "4002-S01 Sander", "parentAssetID": 13042, "parentID": 900},
                ]

        with tempfile.TemporaryDirectory() as tmp:
            repo = RawRepository(Path(tmp) / "gremlin.db")
            service = IngestionService(
                Client(), repo, refresh_mapping=False, fetch_instructions=False, log=lambda _message: None
            )
            summary = service.sync_all()
            self.assertEqual(summary["stored_assets"], 2)
            with repo.connect() as conn:
                rows = conn.execute("SELECT asset_id, asset_name, parent_asset_id FROM limble_asset ORDER BY asset_id").fetchall()
            self.assertEqual([tuple(row) for row in rows], [
                ("13042", "4002 Panel Finishing System", None),
                ("4101", "4002-S01 Sander", "13042"),
            ])

    def test_an_asset_naming_itself_is_top_level(self):
        self.assertIsNone(_asset_parent_id({"assetID": 5, "parentAssetID": 5}))
        self.assertIsNone(_asset_parent_id({"assetID": 5, "parentAssetID": "0"}))


def test_the_api_reads_the_pm_calendars_hierarchy(monkeypatch, tmp_path):
    monkeypatch.setenv("GREMLIN_DB_PATH", str(tmp_path / "gremlin.db"))
    monkeypatch.setenv("GREMLIN_ACCESS_DB_PATH", str(tmp_path / "accesscontrol.db"))
    monkeypatch.setenv("GREMLIN_PM_CALENDAR_DB_PATH", str(tmp_path / "pm.db"))
    import app

    module = importlib.reload(app)
    seed(module.get_life_data_service())
    module.access_control.save_user(None, "editor1", "1357", "editor", "facilities", "engineer")
    client = module.app.test_client()
    with client.session_transaction() as session:
        session["user"] = module.access_control.authenticate("editor1", "1357")

    def offered():
        page = client.get(f"/life-data-analysis/api/dispositions?asset={PFS}").get_json()
        return [option["asset_number"] for option in page["options"]["sub_assets"]]

    # No PM calendar yet: the work orders' own parents, and no calendar file made.
    assert offered() == [SANDER, BOILER]
    assert not (tmp_path / "pm.db").exists()
    module.pm_calendar_service.repo.ensure_schema()
    module.pm_calendar_service.repo.replace_assets(PM_HIERARCHY)
    assert offered() == [SANDER, OVEN, BURNER, WATER]
