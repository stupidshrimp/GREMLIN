"""Seed values for the Availability Dashboard.

These are the workbook's own configuration, transcribed from the Configurator
sheet of `Availability_Dashboard_V6TEST.xlsm`. They seed a database that has no
availability configuration yet, and back the "reset to defaults" action. Once
seeded, the database is authoritative -- editing these constants does not
disturb a configured installation.

Group rows are ``(name, assets, schedule_h, break_h, lunch_h, setup_h, include,
notes)``; net hours per day is always derived, never stored here.
"""

from __future__ import annotations

# The plant's local timezone. Work orders are stored in UTC and bucketed into
# months in local time, so this decides which month a work order created near
# midnight belongs to. The workbook used a flat 5-hour subtraction year-round,
# which is right for only half the year in any US timezone.
DEFAULT_TIMEZONE = "America/Chicago"

# Configurator!A9:I18. "Dilo & Enervac" is carried with include=False and no
# assets, exactly as the workbook has it -- nine charts, not ten.
DEFAULT_ASSET_GROUPS: tuple[tuple, ...] = (
    ("Salvagnini", ("3101", "3102", "3103", "3104", "3105", "3106", "3107"),
     24, 1, 2, 3, True, "3101-3107"),
    ("Building 12 Cloos Robots", ("2743", "2744", "2745", "2746"),
     20, 1, 1, 2, True, "R1 2743, R2 2744, R3 2745, R4 2746"),
    ("Building 6 Finishing", ("4001", "4002"),
     20, 1, 1, 2, True, "EFS 4001, PFS 4002"),
    ("Building 9 Plating Lines", ("1935", "1934", "4000"),
     20, 1, 1, 2, True, "Bright Dip 1935, Silver 1934, Zinc 4000"),
    ("Building 6 LVDs and Press Brakes", ("3147", "3150", "2499", "3028", "2689"),
     20, 1, 1, 2, True, "LVD 3147, LVD 3150, Cincinnati Press 2499, 3028, 2689"),
    ("Building 5 Mazak Lasers", ("3000", "2728"),
     20, 1, 1, 2, True, "Mazak Laser 3000, Mazak Laser 2728"),
    ("Building 1 Secondary Finishing", ("505", "1682", "4028", "758", "3326", "2667", "987"),
     20, 1, 1, 2, True,
     "Tumbler 505, Rumped Tumbler 1682, Ransohoff 4028, Metco Silver 758, Vapor Blast 3326, Vibetech 2667"),
    ("PPD Hedrich Dispensers", ("3154", "3142", "3023", "3253"),
     24, 1, 2, 3, True, "H3 3154, H2 3142, H1 3023, H4 3253"),
    ("PPD Sandblasters", ("3359", "3461", "3325", "3160", "2958", "3073"),
     20, 1, 1, 2, True,
     "Bushing 3359, PME Retrofit 3461, Edge Restore 3325, Shield 3160, ATC Sensor 2958, Vista SD 3073"),
    ("Dilo & Enervac", (),
     20, 0, 0, 0, False, "TBD; Limble asset list to be corrected"),
)

# Configurator!F32:G71 -- the short names the charts label their bars with.
DEFAULT_DISPLAY_NAMES: dict[str, str] = {
    "3101": "MV", "3102": "PA", "3103": "L3", "3104": "ADL",
    "3105": "S4", "3106": "SMD", "3107": "ACN",
    "2743": "Cloos 1", "2744": "Cloos 2", "2745": "Cloos 3", "2746": "Cloos 4",
    "4001": "EFS", "4002": "PFS",
    "1935": "Bright Dip Line", "1934": "Silver Line", "4000": "Zinc",
    "3147": "LVD3147", "3150": "LVD3150", "2499": "CP2499", "3028": "CP3028", "2689": "CP2689",
    "3000": "Mazak Laser 3000", "2728": "Mazak Laser 2728",
    "505": "Tumbler 505", "1682": "Rumped Tumbler 1682", "4028": "Ransohoff 4028",
    "758": "Metco Silver 758", "3326": "Vapor Blast 3326", "2667": "Vibetech Vibratory 2667",
    "987": "Pangborn 987",
    "3154": "H3 3154", "3142": "H2 3142", "3023": "H1 3023", "3253": "H4 3253",
    "3359": "Bushing 3359", "3461": "PME Retrofit 3461", "3325": "Edge Restore 3325",
    "3160": "Shield 3160", "2958": "ATC Sensor 2958", "3073": "Vista SD 3073",
}

# Configurator!tblLinkedDowntimeConfig (A31:D44). Salvagnini assets are
# mechanically coupled, so a stoppage on one charges half its hours to the
# assets it feeds. Every seeded rule is SALV at 0.5; other groups are
# direct-only until someone configures otherwise.
DEFAULT_LINKED_RULES: tuple[tuple[str, str, str, float], ...] = (
    ("SALV", "3102", "3107", 0.5),
    ("SALV", "3102", "3101", 0.5),
    ("SALV", "3102", "3105", 0.5),
    ("SALV", "3102", "3106", 0.5),
    ("SALV", "3103", "3104", 0.5),
    ("SALV", "3103", "3101", 0.5),
    ("SALV", "3104", "3101", 0.5),
    ("SALV", "3105", "3101", 0.5),
    ("SALV", "3105", "3106", 0.5),
    ("SALV", "3106", "3101", 0.5),
    ("SALV", "3107", "3105", 0.5),
    ("SALV", "3107", "3106", 0.5),
    ("SALV", "3107", "3101", 0.5),
)
