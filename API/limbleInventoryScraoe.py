import os
import time
import base64
import requests
import pandas as pd

# =========================
# CONFIG
# =========================
CLIENT_ID = "C3NO78QLBO77T43CEU19Y8HRYMII03IM"
CLIENT_SECRET = "SZP0ZUJNZQZ80YYP5IZGECGY0H0OIF1H"
BASE_URL = "https://api.limblecmms.com/v2"

ASSET_ID = 4757
SECONDS_PER_REQUEST = 1.1
PAGE_LIMIT = 200

DOWNLOADS_PATH = os.path.join(os.path.expanduser("~"), "Downloads")
CSV_FILE = os.path.join(DOWNLOADS_PATH, f"asset_{ASSET_ID}_tasks.csv")
EXCEL_FILE = os.path.join(DOWNLOADS_PATH, f"asset_{ASSET_ID}_tasks.xlsx")

# ----------------------
# AUTH
# ----------------------
credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
encoded_credentials = base64.b64encode(credentials.encode()).decode()
headers = {
    "Authorization": f"Basic {encoded_credentials}",
    "Content-Type": "application/json"
}

# =========================
# STEP 1: PAGINATE TASKS AND WRITE TO CSV
# =========================
page = 1
all_tasks = []

print("Pulling tasks with pagination and writing to CSV...")

while True:
    params = {"limit": PAGE_LIMIT, "page": page}
    try:
        resp = requests.get(f"{BASE_URL}/tasks/", headers=headers, params=params)
        if resp.status_code == 429:
            print("Rate limit hit, sleeping 60 seconds...")
            time.sleep(60)
            continue
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print("Error pulling tasks:", e)
        break

    tasks_page = resp.json()
    if not tasks_page:
        print("No more tasks to pull.")
        break

    print(f"Pulled page {page} with {len(tasks_page)} tasks")
    all_tasks.extend(tasks_page)

    # Prepare DataFrame for current page
    df_page = pd.DataFrame([{
        "taskID": t.get("taskID"),
        "assetID": t.get("assetID"),
        "name": t.get("name"),
        "template": t.get("template"),
        "createdDate": pd.to_datetime(t.get("createdDate", 0), unit='s'),
        "dateCompleted": pd.to_datetime(t.get("dateCompleted", 0), unit='s') if t.get("dateCompleted") else None
    } for t in tasks_page])

    # Append to CSV
    if not os.path.exists(CSV_FILE):
        df_page.to_csv(CSV_FILE, index=False)
    else:
        df_page.to_csv(CSV_FILE, index=False, mode='a', header=False)

    page += 1
    time.sleep(SECONDS_PER_REQUEST)

print(f"\nTotal tasks retrieved: {len(all_tasks)}")
print(f"CSV file saved at: {CSV_FILE}")

# =========================
# STEP 2: FILTER TASKS FOR SPECIFIC ASSET AND WRITE EXCEL
# =========================
df_all = pd.read_csv(CSV_FILE)
tasks_for_asset = df_all[(df_all["assetID"] == ASSET_ID) & (~df_all["template"])]

if not tasks_for_asset.empty:
    with pd.ExcelWriter(EXCEL_FILE, engine='openpyxl') as writer:
        # All tasks sheet
        df_all.to_excel(writer, index=False, sheet_name="All_Tasks")
        # Filtered asset sheet
        tasks_for_asset.to_excel(writer, index=False, sheet_name=f"Asset_{ASSET_ID}")
    print(f"Excel file saved at: {EXCEL_FILE}")
else:
    print(f"No real tasks found for asset {ASSET_ID}")



