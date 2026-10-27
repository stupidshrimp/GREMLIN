"""A synthetic plant's work orders, written the way technicians write them, for the disposition helper.

Each scenario is one way a part of the 4002 paint line fails (or is serviced),
with the disposition an engineer gives it. The work orders are written from it
with the variety real ones have: different titles for the same fault, a
narrative that is sometimes left empty, abbreviations, the odd typo, and notes
that say what was done in other words. The disposition is the scenario's, so a
test knows what the helper should say.

Not a test module itself (no ``test_`` prefix): the helper's tests and the
accuracy check build their databases from it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

PFS = "4002"
PFS_NAME = "Panel Finishing System"
SUB_ASSETS = {
    "4002-S01": "Pretreatment washer",
    "4002-S05": "Activator stage",
    "4002-S09": "Dry-off oven",
    "4002-S11": "Prime booth",
    "4002-S12": "Prime bake oven",
    "4002-S14": "Oven conveyor",
    "4002-S16": "Powder booth",
    "4002-S18": "Cure oven",
}
ASSET_IDS = {PFS: "100", **{number: str(200 + index) for index, number in enumerate(SUB_ASSETS)}}


@dataclass(frozen=True)
class Scenario:
    key: str
    sub_asset: str | None
    category: str
    component: str
    mechanism: str
    mode: str
    action: str
    titles: tuple[str, ...]
    conditions: tuple[str, ...]
    actions: tuple[str, ...]
    weight: float = 1.0
    record_class: str = "CORRECTIVE_WO"
    renewed: tuple[str, ...] = ()


SCENARIOS = (
    Scenario(
        "s09-filter", "4002-S09", "FAILURE", "Filter", "CONT", "FTS", "CLEAN_SERVICE",
        ("Dry-off oven won't light", "dry off oven not lighting", "S09 burner wont fire", "Dry off oven down no flame",
         "dryoff oven will not ignite"),
        ("combustion air filter clogged with dust", "filter loaded up with powder", "air filter dirty, burner starved",
         "intake filter plugged", "filter covered in dust"),
        ("cleaned filter and relit burner", "blew out filter, relit", "cleaned the intake filter, oven back up",
         "vacuumed filter and reset", "cleaned filter"),
        weight=3.0,
    ),
    Scenario(
        "s09-flamerod", "4002-S09", "FAILURE", "Burner", "SIGN", "UST", "CLEAN_SERVICE",
        ("Dry-off oven tripped out", "S09 flame failure", "dry off oven keeps tripping on flame loss",
         "dry-off burner shutting down mid shift"),
        ("weak flame signal, flame rod dirty", "flame rod coated, low microamps", "flame sensor signal low"),
        ("cleaned flame rod, signal back to 4 uA", "polished flame rod and reset", "cleaned flame sensor"),
        weight=2.0,
    ),
    Scenario(
        "s12-igniter", "4002-S12", "FAILURE", "Burner", "WEAR", "FTS", "REPLACE",
        ("Prime bake oven won't light", "prime bake burner not lighting", "S12 no ignition",
         "prime bake oven will not fire"),
        ("igniter worn out", "ignitor tip eroded", "spark rod worn, no spark"),
        ("replaced igniter", "installed new ignitor, oven lit", "swapped spark rod"),
        weight=2.0,
    ),
    Scenario(
        "s14-chain", "4002-S14", "FAILURE", "Chain", "WEAR", "VIB", "ADJUST",
        ("Oven conveyor chain noisy", "conveyor chain jumping/banging", "S14 chain popping",
         "loud noise from oven conveyor chain"),
        ("chain stretched", "chain worn and stretched, slack at take up", "excess slack in chain"),
        ("adjusted take-up", "tightened chain take up", "took up slack on chain"),
        weight=2.0,
    ),
    Scenario(
        "s14-bearing", "4002-S14", "FAILURE", "Bearing", "WEAR", "VIB", "REPLACE",
        ("Conveyor drive bearing grinding", "oven conveyor drive making grinding noise", "S14 drive bearing loud",
         "conveyor drive bearing squealing"),
        ("drive bearing worn, rough", "bearing dry and worn", "pillow block bearing failing"),
        ("replaced drive bearing", "changed out pillow block bearing", "installed new bearing and greased"),
        weight=1.5,
    ),
    Scenario(
        "s16-gun", "4002-S16", "FAILURE", "Gun", "PLUG", "LOO", "CLEAN_SERVICE",
        ("Powder gun not spraying", "powder booth gun spitting/low output", "S16 gun weak pattern",
         "low powder output gun 3"),
        ("gun tip clogged with powder", "powder impacted in gun", "nozzle plugged on gun"),
        ("cleaned gun and tip", "blew out gun, cleaned nozzle", "cleaned powder gun"),
        weight=2.0,
    ),
    Scenario(
        "s16-hose", "4002-S16", "FAILURE", "Hose", "LEAK", "LKG", "REPLACE",
        ("Air leak powder booth", "powder hose leaking", "S16 hose blowing powder",
         "leak at powder feed hose"),
        ("powder hose cracked and leaking", "split in feed hose", "hose worn through"),
        ("replaced powder hose", "installed new feed hose", "changed hose"),
    ),
    Scenario(
        "s01-seal", "4002-S01", "FAILURE", "Seal", "LEAK", "LKG", "REPLACE",
        ("Washer pump leaking", "stage 1 pump leaking water", "S01 pump seal leak", "washer pump dripping"),
        ("mechanical seal leaking", "pump seal failed", "seal worn, leaking at shaft"),
        ("replaced pump seal", "rebuilt pump with new mechanical seal", "changed seal"),
        weight=1.5,
    ),
    Scenario(
        "s01-nozzle", "4002-S01", "FAILURE", "Nozzle", "PLUG", "LOO", "CLEAN_SERVICE",
        ("Washer nozzles plugged", "poor spray coverage in washer", "S01 risers clogged",
         "washer low spray pressure"),
        ("risers plugged with scale", "nozzles blocked with debris", "spray nozzles clogged"),
        ("cleaned risers and nozzles", "pulled and cleaned nozzles", "flushed risers"),
        weight=1.5,
    ),
    Scenario(
        "s05-fuse", "4002-S05", "FAILURE", "Electrical", "ELEC", "FTS", "REPLACE",
        ("Activator pump not running", "activator stage pump won't start", "S05 pump dead",
         "activator pump not pumping"),
        ("blown fuse in starter bucket", "fuse blown", "lost a phase, blown fuse"),
        ("replaced fuses", "changed blown fuse, pump running", "replaced fuse and reset"),
        weight=1.5,
    ),
    Scenario(
        "s11-belt", "4002-S11", "FAILURE", "Belt", "BRKN", "UST", "REPLACE",
        ("Prime booth exhaust fan down", "booth exhaust not pulling", "S11 exhaust fan belt broke",
         "prime booth fan stopped"),
        ("fan belt broken", "belt snapped", "drive belt broke"),
        ("replaced belt", "installed new belts and tensioned", "put on new v-belt"),
        weight=1.5,
    ),
    Scenario(
        "s18-thermocouple", "4002-S18", "FAILURE", "Sensor", "SIGN", "ERO", "REPLACE",
        ("Cure oven temp reading erratic", "cure oven temperature jumping", "S18 bad temp reading",
         "cure oven showing wrong temp"),
        ("thermocouple bad", "thermocouple reading open intermittently", "faulty thermocouple"),
        ("replaced thermocouple", "installed new thermocouple", "changed tc"),
        weight=1.5,
    ),
    Scenario(
        "s18-contactor", "4002-S18", "FAILURE", "Starter", "ELEC", "UST", "REPLACE",
        ("Cure oven blower tripping", "cure oven circ fan kicks out", "S18 blower tripped overload",
         "cure oven recirc blower trips"),
        ("contactor contacts burnt", "contactor pitted and burnt", "overload relay bad"),
        ("replaced contactor", "replaced contactor and overload", "changed out starter"),
    ),
    Scenario(
        "plc", None, "FAILURE", "Controller", "SIGN", "UST", "RESET_ONLY",
        ("Line stopped - PLC fault", "line down on PLC fault", "PLC faulted, line stopped", "HMI fault line stop"),
        ("processor fault on PLC", "comm fault on PLC rack", "PLC lost communication"),
        ("reset PLC fault", "cycled power to PLC, reset", "cleared fault and restarted line"),
    ),
    Scenario(
        "gas-off", None, "EXTERNAL", "GasTrain", "UNK", "FTS", "RESET_ONLY",
        ("Line down - main gas off", "no gas to ovens", "ovens down, gas supply off", "gas off to building"),
        ("gas utility shut off upstream", "plant gas supply off", "gas company work, supply off"),
        ("relit ovens after gas restored", "reset burners once gas back on", "restarted ovens"),
        weight=1.0,
    ),
    Scenario(
        "power", None, "EXTERNAL", "Electrical", "UNK", "FTS", "RESET_ONLY",
        ("Power outage - line down", "power bump took line down", "plant power failure", "lost power to line"),
        ("utility power outage", "power blip from utility", "storm power outage"),
        ("restarted line after power returned", "reset all drives and restarted", "restarted equipment"),
    ),
    Scenario(
        "check-filter", "4002-S09", "NON_FAILURE", "Filter", "UNK", "NFF", "INSPECT_ONLY",
        ("Check dry-off oven filter", "inspect dry off filter", "S09 check filter condition"),
        ("filter ok", "filter looks good", "no problem found"),
        ("checked filter, ok", "inspected, no issues", "checked, working fine"),
        weight=1.0,
    ),
    Scenario(
        "check-booth", "4002-S16", "NON_FAILURE", "Fan", "UNK", "NFF", "INSPECT_ONLY",
        ("Verify powder booth airflow", "check booth exhaust airflow", "S16 airflow check"),
        ("airflow within spec", "no fault found", "readings normal"),
        ("verified airflow ok", "tested ok", "checked, running normally"),
    ),
    Scenario(
        "pm-filters", "4002-S09", "PM_RESET", "Filter", "WEAR", "DEG", "CLEAN_SERVICE",
        ("S09 Monthly PM - change dry-off oven filters", "Monthly PM dry off oven filters", "S09 monthly filter PM"),
        ("filters dirty as expected", "filters loaded, due", ""),
        ("changed filters", "replaced all filters", "PM complete, filters changed"),
        weight=2.0, record_class="PM", renewed=("Filter",),
    ),
    Scenario(
        "pm-chain", "4002-S14", "PM_RESET", "Chain", "WEAR", "DEG", "CLEAN_SERVICE",
        ("Weekly PM - oven conveyor lube", "S14 weekly chain lube PM", "Weekly conveyor chain lubrication"),
        ("chain dry", "", "chain ok, lubed"),
        ("lubed chain", "lubricated conveyor chain", "PM complete, chain greased"),
        weight=2.0, record_class="PM", renewed=("Chain",),
    ),
)

# The same faults in other words, none of them the templates' own phrasing: the
# work orders a plant writes after the ones it has dispositioned. Reading these
# right needs meaning, not shared words. Keyed by scenario: (titles,
# conditions, actions).
OTHER_WORDS: dict[str, tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = {
    "s09-filter": (
        ("No flame at the dry-off oven on startup", "Drying oven burner fails to ignite", "Dry-off oven stays cold, burner out"),
        ("intake screen caked with dust", "combustion intake choked with debris", "inlet screen packed solid"),
        ("vacuumed the intake screen, burner back on", "cleared the inlet screen and restarted", "washed out the intake screen"),
    ),
    "s09-flamerod": (
        ("Drying oven keeps losing flame mid-run", "Dry-off oven flame lockout during production"),
        ("flame sensor coated with residue, signal weak", "sensing rod fouled"),
        ("polished the flame sensor, signal good", "wiped the sensing rod and restarted"),
    ),
    "s12-igniter": (
        ("Prime bake oven has no ignition at start-up", "Prime bake burner not firing at all"),
        ("spark electrode eroded away", "electrode tip burnt back, no spark"),
        ("fitted a new spark electrode", "new electrode installed, burner lit"),
    ),
    "s14-chain": (
        ("Oven conveyor clanking", "Rattling from the oven conveyor drive chain"),
        ("chain elongated, too much slack", "chain sagging at the take-up"),
        ("took up the slack", "re-tensioned the chain"),
    ),
    "s14-bearing": (
        ("Oven conveyor drive howling", "Screeching from the conveyor drive end"),
        ("drive bearing rough and dry", "bearing rumbling, play in the shaft"),
        ("fitted a new drive bearing", "new bearing installed and lubricated"),
    ),
    "s16-gun": (
        ("Powder booth gun sputtering", "Gun 3 throwing a thin uneven pattern"),
        ("powder packed in the gun tip", "tip choked with fused powder"),
        ("stripped and cleaned the gun tip", "cleared the gun and blew it through"),
    ),
    "s16-hose": (
        ("Powder escaping from the booth feed line", "Feed line spitting powder at the booth"),
        ("feed line split", "line chafed through"),
        ("fitted a new feed line", "new line run to the gun"),
    ),
    "s01-seal": (
        ("Water running from the washer pump", "Pretreatment pump weeping at the shaft"),
        ("shaft seal worn out", "gland seal gone"),
        ("installed a new shaft seal", "new gland seal fitted"),
    ),
    "s01-nozzle": (
        ("Weak spray inside the pretreatment washer", "Washer coverage patchy"),
        ("jets choked with scale", "spray tips crusted over"),
        ("descaled the spray tips", "soaked and cleared the jets"),
    ),
    "s05-fuse": (
        ("Activator pump completely dead", "No power to the activator pump"),
        ("fuse open in the bucket", "one fuse gone, lost a leg"),
        ("swapped the fuses, pump runs", "new fuses fitted"),
    ),
    "s11-belt": (
        ("No extraction in the prime booth", "Prime booth extraction stopped pulling air"),
        ("drive belt in pieces", "belt shredded"),
        ("fitted new drive belts", "new belts on and tensioned"),
    ),
    "s18-thermocouple": (
        ("Cure oven temperature bouncing around", "Cure oven showing crazy temperatures"),
        ("temperature probe failing open", "probe reading open intermittently"),
        ("fitted a new temperature probe", "probe changed"),
    ),
    "s18-contactor": (
        ("Cure oven circulation fan keeps cutting out", "Cure oven recirculation fan drops off"),
        ("contacts pitted", "contact tips welded and pitted"),
        ("installed a new contactor", "new contactor fitted"),
    ),
    "plc": (
        ("Whole line halted on a controller fault", "Line halted, processor error"),
        ("processor in fault", "controller lost its rack"),
        ("cleared the processor fault and restarted", "acknowledged the fault, line running"),
    ),
    "gas-off": (
        ("Ovens out, no natural gas supply", "Gas utility outage took the ovens out"),
        ("supply shut off by the gas company", "utility gas interrupted"),
        ("restarted the ovens when supply returned", "brought the ovens back once gas was back"),
    ),
    "power": (
        ("Line dead after a utility power loss", "Blackout stopped the line"),
        ("utility blackout", "outage on the incoming supply"),
        ("brought the line back up after the outage", "restarted everything once power was back"),
    ),
    "check-filter": (
        ("Look over the dry-off oven intake screen", "Inspect the drying oven inlet screen"),
        ("looks fine", "in good shape"),
        ("inspected, all good", "looked at it, nothing to do"),
    ),
    "check-booth": (
        ("Measure powder booth air velocity", "Confirm powder booth extraction"),
        ("within limits", "velocities fine"),
        ("measured, all good", "confirmed, nothing wrong"),
    ),
    "pm-filters": (
        ("Monthly dry-off oven intake screen change", "Drying oven monthly screen swap"),
        ("screens loaded as expected", ""),
        ("swapped all screens", "fitted fresh screens"),
    ),
    "pm-chain": (
        ("Weekly oven conveyor greasing", "Oven conveyor chain greasing round"),
        ("chain dry", ""),
        ("greased the chain", "chain greased all round"),
    ),
}

TYPOS = {"filter": "fliter", "bearing": "bearng", "replaced": "replced", "conveyor": "conveyer", "thermocouple": "thermocoupl",
         "cleaned": "clened", "contactor": "contacter", "igniter": "ignitor"}
NARRATIVE_EMPTY = 0.3
# Work orders that say next to nothing ("4002 down", "fixed"): the engineer knew
# from elsewhere, the text cannot tell.
VAGUE_TITLES = ("Line issue", "4002 down", "see notes", "Maintenance request", "Problem on line", "line stopped")
VAGUE_NOTES = ("fixed", "done", "repaired", "ok now", "")


def _typo(phrase: str, rng: random.Random) -> str:
    words = phrase.split()
    for index, word in enumerate(words):
        if word.lower() in TYPOS and rng.random() < 0.15:
            words[index] = TYPOS[word.lower()]
    return " ".join(words)


def work_order(scenario: Scenario, rng: random.Random, *, vague: bool = False, other_words: bool = False) -> dict[str, Any]:
    """One work order written from a scenario (or, ``vague``, one that hardly says anything).

    ``other_words`` writes it in OTHER_WORDS rather than the scenario's own phrasing.
    """

    if vague:
        title = rng.choice(VAGUE_TITLES)
        return {"task_name": title, "request_title": title, "requestor_description": "", "condition_found": None,
                "cause": None, "action_taken": None, "completion_notes": rng.choice(VAGUE_NOTES)}
    titles, conditions, actions = (
        OTHER_WORDS[scenario.key] if other_words else (scenario.titles, scenario.conditions, scenario.actions)
    )
    title = _typo(rng.choice(titles), rng)
    condition = _typo(rng.choice(conditions), rng)
    action = _typo(rng.choice(actions), rng)
    empty = rng.random() < NARRATIVE_EMPTY
    notes = action if empty else rng.choice(["", f"{action}. ok", f"{condition}; {action}", "done"])
    return {
        "task_name": title,
        "request_title": title,
        "requestor_description": rng.choice(["", title, f"{title}, please look at it", "see title"]),
        "condition_found": None if empty else condition,
        "cause": None if empty or rng.random() < 0.5 else rng.choice(["wear", "unknown", "dirt", condition]),
        "action_taken": None if empty else action,
        "completion_notes": notes,
    }


def disposition(scenario: Scenario) -> dict[str, Any]:
    values = {
        "disposition_category": scenario.category,
        "sub_asset": scenario.sub_asset,
        "component_code": scenario.component,
        "failure_mechanism_code": scenario.mechanism,
        "failure_mode_code": scenario.mode,
        "action_code": scenario.action,
        "evidence": "INFERRED",
    }
    if scenario.renewed:
        values["renewed_items"] = list(scenario.renewed)
    return values


def setup_tables(life) -> None:
    with life.write_connection() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS import_batch (import_batch_id INTEGER PRIMARY KEY, status TEXT)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS raw_cmms_record ("
            "raw_record_id INTEGER PRIMARY KEY, import_batch_id INTEGER NOT NULL DEFAULT 1, raw_json TEXT NOT NULL)"
        )
        conn.execute("INSERT OR IGNORE INTO import_batch (import_batch_id, status) VALUES (1, 'COMPLETED')")


def insert_records(life, records: list[dict[str, Any]]) -> None:
    """Work orders as the Limble sync maps them: ``mapped_record_id``, ``asset_number`` and the text."""

    with life.write_connection() as conn:
        conn.executemany(
            "INSERT INTO raw_cmms_record (raw_record_id, import_batch_id, raw_json) VALUES (?, 1, '{}')",
            [(record["mapped_record_id"],) for record in records],
        )
        conn.executemany(
            """
            INSERT INTO mapped_cmms_record (
                mapped_record_id, raw_record_id, import_batch_id, asset_number, asset_name, asset_id_raw,
                immediate_parent_asset_id, task_id, task_name, request_title, requestor_description, condition_found,
                cause, action_taken, completion_notes, created_date_final, completed_date_final, downtime_hours,
                record_class_auto, mapping_version
            ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'v4')
            """,
            [
                (
                    record["mapped_record_id"], record["mapped_record_id"], record["asset_number"], record["asset_name"],
                    record["asset_id"], record.get("parent_asset_id"), str(record["mapped_record_id"]),
                    record["task_name"], record["request_title"], record["requestor_description"],
                    record["condition_found"], record["cause"], record["action_taken"], record["completion_notes"],
                    record["created"], record["created"], record.get("downtime", 0.0), record["record_class"],
                )
                for record in records
            ],
        )


def build_plant(life, *, per_scenario: int = 8, seed: int = 4002, start_id: int = 1, other_assets: int = 0,
                other_per_asset: int = 0, vague: float = 0.0, mislabel: float = 0.0,
                other_words_after: float | None = None) -> dict[int, dict[str, Any]]:
    """Fill a GREMLIN.db with the 4002 line's work orders (and, optionally, other assets' to bulk it out).

    Returns each 4002 work order's intended disposition by mapped_record_id, in
    the order they were written (oldest first). ``vague`` is the share of work
    orders that hardly say anything, and ``mislabel`` the share whose intended
    disposition is another scenario's (engineers disagree too). From the share
    ``other_words_after`` of the line's work orders on, they are written in
    OTHER_WORDS: a backlog worded unlike anything dispositioned before it.
    """

    rng = random.Random(seed)
    setup_tables(life)
    records: list[dict[str, Any]] = []
    truth: dict[int, dict[str, Any]] = {}
    next_id = start_id
    day = date(2023, 1, 2)
    # One work order on each sub-asset's own record, as Limble files them: this
    # is how the hierarchy knows the sub-assets and their names.
    for number, name in SUB_ASSETS.items():
        records.append({
            "mapped_record_id": next_id, "asset_number": number, "asset_name": f"{number} {name}", "asset_id": ASSET_IDS[number],
            "parent_asset_id": ASSET_IDS[PFS], "task_name": f"{name} housekeeping", "request_title": None,
            "requestor_description": None, "condition_found": None, "cause": None, "action_taken": None,
            "completion_notes": None, "created": day.isoformat(), "record_class": "ADMINISTRATIVE",
        })
        next_id += 1
    pool = [scenario for scenario in SCENARIOS for _ in range(max(1, round(scenario.weight * per_scenario)))]
    rng.shuffle(pool)
    switch = None if other_words_after is None else int(len(pool) * other_words_after)
    for index, scenario in enumerate(pool):
        day += timedelta(days=rng.choice([0, 1, 1, 2, 3]))
        written = work_order(scenario, rng, vague=rng.random() < vague,
                             other_words=switch is not None and index >= switch)
        records.append({
            "mapped_record_id": next_id, "asset_number": PFS, "asset_name": f"{PFS} {PFS_NAME}", "asset_id": ASSET_IDS[PFS],
            **written, "created": f"{day.isoformat()}T{rng.randint(6, 22):02d}:00:00+00:00",
            "record_class": scenario.record_class,
        })
        decided = rng.choice(SCENARIOS) if rng.random() < mislabel else scenario
        truth[next_id] = {"scenario": decided.key, **disposition(decided)}
        next_id += 1
    for asset_index in range(other_assets):
        number = f"{5000 + asset_index}"
        for _ in range(other_per_asset):
            scenario = rng.choice(SCENARIOS)
            written = work_order(scenario, rng)
            for key in ("task_name", "request_title"):
                # Another asset's own words: the same faults, not on the 4002 line.
                written[key] = written[key].replace("Dry-off", "Line").replace("dry off", "line").replace("S09", "")
            records.append({
                "mapped_record_id": next_id, "asset_number": number, "asset_name": f"Machine {number}",
                "asset_id": f"9{asset_index:04d}", **written,
                "created": (date(2022, 1, 1) + timedelta(days=rng.randint(0, 1400))).isoformat(),
                "record_class": scenario.record_class,
            })
            next_id += 1
    insert_records(life, records)
    return truth
