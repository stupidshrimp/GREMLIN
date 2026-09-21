from services.sync_service import LIMBLE_ENV_PREFIX, load_dotenv_files
from integrations.limble import LimbleClient, LimbleConfig
import json

load_dotenv_files(only_prefix=LIMBLE_ENV_PREFIX)
client = LimbleClient(LimbleConfig.from_env(page_limit=1000))  # bumped up, you've proven the account can handle it

def on_page(items_so_far, pages_read):
    print(f"...fetched {items_so_far} tasks so far ({pages_read} page(s))")

tasks = client.get_tasks(on_page=on_page)
print(f"\ntotal tasks: {len(tasks)}")

def is_template(t):
    v = t.get("template")
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "y", "t")
    return bool(v)

template_tasks = [t for t in tasks if is_template(t)]
print(f"tasks with template truthy, ANY asset: {len(template_tasks)}")

asset_3103 = [t for t in tasks if str(t.get("assetID")) == "3103"]
print(f"tasks for asset 3103, any type: {len(asset_3103)}")

pm_3103 = [t for t in asset_3103 if str(t.get("type")) == "1"]
print(f"PM-type tasks for asset 3103: {len(pm_3103)}")

print("\n--- raw JSON: one arbitrary task, to see the full field list ---")
print(json.dumps(tasks[0], indent=2))

if pm_3103:
    print("\n--- raw JSON: one real PM occurrence for asset 3103 ---")
    print(json.dumps(pm_3103[0], indent=2))

if template_tasks:
    print("\n--- raw JSON: one template=true task, any asset ---")
    print(json.dumps(template_tasks[0], indent=2))