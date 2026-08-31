# from services.pm_calendar_service import PmCalendarService
#
# service = PmCalendarService("pm_calendar_test.db")
# print(service.status())        # {'state': 'idle'}
# print(service.asset_options())  # []  (nothing synced yet)


import time
from services.pm_calendar_service import PmCalendarService

service = PmCalendarService(r"C:\Users\billy.trinh\OneDrive - S & C Electric Company\Documents\GREMLINVM\PM_Calendar_local.db")
service.start_sync()

while service.status()["state"] == "running":
    print(service.status())
    time.sleep(2)

print("Final status:", service.status())
print("Assets found:", service.asset_options())