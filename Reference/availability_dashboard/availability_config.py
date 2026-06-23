"""Default Availability Dashboard configuration seeds."""

from __future__ import annotations

from datetime import date

DEFAULT_SETTINGS = {
    "id": 1,
    "selected_year": date.today().year,
    "utc_offset_hours": 5.0,
}

DEFAULT_ASSET_GROUPS = [
    ("Salvagnini", ["3101", "3102", "3103", "3104", "3105", "3106", "3107"], 24, 1, 2, 3, 1, "3101-3107"),
    ("Building 12 Cloos Robots", ["2743", "2744", "2745", "2746"], 20, 1, 1, 2, 1, "R1 2743, R2 2744, R3 2745, R4 2746"),
    ("Building 6 Finishing", ["4001", "4002"], 20, 1, 1, 2, 1, "EFS 4001, PFS 4002"),
    ("Building 9 Plating Lines", ["1935", "1934", "4000"], 20, 1, 1, 2, 1, "Bright Dip 1935, Silver 1934, Zinc 4000"),
    ("Building 6 LVDs and Press Brakes", ["3147", "3150", "2499", "3028", "2689"], 20, 1, 1, 2, 1, "LVD 3147, LVD 3150, Cincinnati Press 2499, 3028, 2689"),
    ("Building 5 Mazak Lasers", ["3000", "2728"], 20, 1, 1, 2, 1, "Mazak Laser 3000, Mazak Laser 2728"),
    ("Building 1 Secondary Finishing", ["505", "1682", "4028", "758", "3326", "2667", "987"], 20, 1, 1, 2, 1, "Tumbler 505, Rumped Tumbler 1682, Ransohoff 4028, Metco Silver 758, Vapor Blast 3326, Vibetech 2667, Pangborn 987"),
    ("PPD Hedrich Dispensers", ["3154", "3142", "3023", "3253"], 24, 1, 2, 3, 1, "H3 3154, H2 3142, H1 3023, H4 3253"),
    ("PPD Sandblasters", ["3359", "3461", "3325", "3160", "2958", "3073"], 20, 1, 1, 2, 1, "Bushing 3359, PME Retrofit 3461, Edge Restore 3325, Shield 3160, ATC Sensor 2958, Vista SD 3073"),
    ("Dilo & Enervac", [], 20, 0, 0, 0, 0, "TBD; Limble asset list to be corrected"),
]

DEFAULT_DISPLAY_NAMES = {
    "3101": "MV", "3102": "PA", "3103": "L3", "3104": "ADL", "3105": "S4", "3106": "SMD", "3107": "ACN",
    "2743": "Cloos 1", "2744": "Cloos 2", "2745": "Cloos 3", "2746": "Cloos 4",
    "4001": "EFS", "4002": "PFS", "1935": "Bright Dip Line", "1934": "Silver Line", "4000": "Zinc",
    "3147": "LVD3147", "3150": "LVD3150", "2499": "CP2499", "3028": "CP3028", "2689": "CP2689",
    "3000": "Mazak Laser 3000", "2728": "Mazak Laser 2728", "505": "Tumbler 505", "1682": "Rumped Tumbler 1682",
    "4028": "Ransohoff 4028", "758": "Metco Silver 758", "3326": "Vapor Blast 3326", "2667": "Vibetech Vibratory 2667", "987": "Pangborn 987",
    "3154": "H3 3154", "3142": "H2 3142", "3023": "H1 3023", "3253": "H4 3253", "3359": "Bushing 3359",
    "3461": "PME Retrofit 3461", "3325": "Edge Restore 3325", "3160": "Shield 3160", "2958": "ATC Sensor", "3073": "Vista SD",
}

DEFAULT_LINKED_RULES = [
    ("SALV", "3102", "3107", 0.5), ("SALV", "3102", "3101", 0.5), ("SALV", "3102", "3105", 0.5), ("SALV", "3102", "3106", 0.5),
    ("SALV", "3103", "3104", 0.5), ("SALV", "3103", "3101", 0.5), ("SALV", "3104", "3101", 0.5),
    ("SALV", "3105", "3101", 0.5), ("SALV", "3105", "3106", 0.5), ("SALV", "3106", "3101", 0.5),
    ("SALV", "3107", "3105", 0.5), ("SALV", "3107", "3106", 0.5), ("SALV", "3107", "3101", 0.5),
]
