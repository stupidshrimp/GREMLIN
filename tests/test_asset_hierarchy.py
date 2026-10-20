"""An asset's sub-assets: read from Limble's parentAssetID, never parentID.

On this account an asset's number is its Limble id, and Limble sends a
``parentID`` on top-level assets that is not their parent. Work orders imported
while the sync still fell back to it name another top-level asset as the parent
of a top-level asset, so the Disposition page offered whole other machines as an
asset's sub-assets. The PM calendar keeps the hierarchy by ``parentAssetID``
alone; the asset tree takes it from there wherever it knows an asset.
"""

import importlib
import tempfile
import unittest
from pathlib import Path

from services.ingestion_service import AssetIndex, _asset_parent_id
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
        # What the page used to offer: a boiler as a sub-asset of the paint line,
        # and two top-level machines under the water system.
        self.assertEqual(self.numbers(tree, PFS), [(SANDER, 1), (BOILER, 1)])
        self.assertEqual({number for number, _ in self.numbers(tree, WATER)}, {PFS, COMPRESSOR, BOILER, SANDER})

    def test_the_pm_calendars_hierarchy_wins_wherever_it_knows_the_asset(self):
        with self.life.connect() as conn:
            tree = AssetTree.load(conn, PM_HIERARCHY)
        # Every level down, in name order; the oven has no work orders of its own
        # yet and is still offered, with the burner under it.
        self.assertEqual(self.numbers(tree, PFS), [(SANDER, 1), (OVEN, 1), (BURNER, 2)])
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
        self.assertEqual(offered, [SANDER, OVEN, BURNER])
        # The boiler's work order is not in reach of the paint line any more.
        analysis = PopulationAnalysisService(self.life, records)
        downtime = analysis.downtime({"assets": [PFS], "categories": ["NOT_REVIEWED"]})
        self.assertEqual(downtime["summary"]["work_order_count"], 3)
        options = analysis.options([PFS])
        self.assertEqual([option["asset_number"] for option in options["sub_assets"]], [SANDER, OVEN, BURNER])

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
    assert offered() == [SANDER, OVEN, BURNER]
