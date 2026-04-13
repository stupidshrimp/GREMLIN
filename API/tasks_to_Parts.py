import os
import time
import base64
import requests
import pandas as pd

# ------------------------
# CONFIG
# ------------------------
CLIENT_ID = "C3NO78QLBO77T43CEU19Y8HRYMII03IM"
CLIENT_SECRET = "SZP0ZUJNZQZ80YYP5IZGECGY0H0OIF1H"
BASE_URL = "https://api.limblecmms.com/v2"
XLSM_FILE = r"YOUR_FILE_PATH.xlsm"
CSV_FILE = r"YOUR_FILE_PATH.csv"
SECONDS_PER_REQUEST = 1.1


# AUTH
# ------------------------
credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
encoded_credentials = base64.b64encode(credentials.encode()).decode()
headers = {
    "Authorization": f"Basic {encoded_credentials}",
    "Content-Type": "application/json"
}

# ------------------------
# READ TASKS
# ------------------------
df_tasks = pd.read_excel(XLSM_FILE, sheet_name="All_Tasks")
tasks_list = df_tasks["taskID"].tolist()
print(f"Total tasks to process: {len(tasks_list)}")

# ------------------------
# PROCESS TASKS
# ------------------------
all_parts = []

for idx, task_id in enumerate(tasks_list, start=1):
    print(f"[{idx}/{len(tasks_list)}] Processing task {task_id}...")

    try:
        resp = requests.get(f"{BASE_URL}/tasks/{task_id}/parts", headers=headers)
        if resp.status_code == 429:  # rate limit
            print("Rate limit hit, sleeping 60 seconds...")
            time.sleep(60)
            resp = requests.get(f"{BASE_URL}/tasks/{task_id}/parts", headers=headers)
        resp.raise_for_status()
        parts = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching parts for task {task_id}: {e}")
        continue

    for part in parts:
        all_parts.append({
            "taskID": task_id,
            "partID": part.get("partID"),
            "partName": part.get("partName"),
            "partNumber": part.get("partNumber"),
            "quantity": part.get("quantity", 0),
            "location": part.get("locationName"),
            "minQty": part.get("minQty"),
            "maxQty": part.get("maxQty"),
            "poItemID": part.get("poItemID"),
            "usedPrice": part.get("usedPrice"),
            "lastEdited": part.get("lastEdited"),
            "relationID": part.get("relationID"),
        })

    # ------------------------
    # SAVE CSV AFTER EACH TASK
    # ------------------------
    pd.DataFrame(all_parts).to_csv(CSV_FILE, index=False)
    time.sleep(SECONDS_PER_REQUEST)

print(f"\nAll parts processed. CSV saved at: {CSV_FILE}")