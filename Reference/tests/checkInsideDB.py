import sqlite3
import pandas as pd

# ✅ Path to DB
db_path = "GREMLIN.db"

# ✅ Tables to export
tables_to_export = [
    "approved_weibull_parameter",
    "asset_failure_mode_option",
    "asset_failure_mechanism_option",
    "sqlite_stat1",
    "availability_settings",
    "availability_asset_groups",
    "sqlite_sequence",
    "availability_asset_group_assets",
    "availability_asset_display_names",
    "availability_linked_downtime_rules",
    "availability_manual_ot",
    "availability_goal_percent",
    "availability_results"
]

# ✅ Output Excel file
output_path = r"C:\Users\billy.trinh\OneDrive - S & C Electric Company\Documents\GREMLINS_DB_Export.xlsx"

# ✅ Connect to DB
conn = sqlite3.connect(db_path)

# ✅ Create Excel writer
with pd.ExcelWriter(output_path, engine="openpyxl") as writer:

    for table in tables_to_export:
        try:
            print(f"Exporting: {table}")

            # ✅ Load table into DataFrame
            df = pd.read_sql_query(f"SELECT * FROM {table} LIMIT 10;", conn)

            # ✅ Write to Excel sheet
            df.to_excel(writer, sheet_name=table[:31], index=False)

        except Exception as e:
            print(f"❌ Error with table {table}: {e}")

# ✅ Close connection
conn.close()

print(f"\n✅ Excel file created: {output_path}")
# # ✅ Save file
# output_path = r"C:\Users\billy.trinh\OneDrive - S & C Electric Company\Documents\GREMLINS_DB_Export.docx"
# doc.save(output_path)
#
# print(f"✅ Word file created: {output_path}")