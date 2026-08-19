"""Ask Limble for one task's instructions and show what GREMLIN makes of them.

The Area Affected / Condition / Cause / Action boxes live in a task's
instructions, and the shape those come back in is the whole question: GREMLIN
reads the prompt from ``instructionText`` and the answer from ``response``,
which is what a task export from the Limble site shows, but an export is not the
API. This answers it directly, in one request, without a sync.

Usage::

    python tools/probe_instructions.py 239728
    python tools/probe_instructions.py 239728 239772 240182
    python tools/probe_instructions.py 239728 --raw     # full payload, verbatim

Credentials come from the same environment variables the sync uses
(``LIMBLE_CLIENT_ID`` / ``LIMBLE_CLIENT_SECRET``, or ``LIMBLE_API_CLIENTID`` /
``LIMBLE_API_KEY``), including from a ``.env`` beside the database.

What to look for: **"narrative found"** listing all four boxes means the reader
works against the live API. Items present but no narrative means the field names
differ from the export -- send the ``--raw`` output and the aliases can be
widened. No items at all means this task genuinely has no instructions; try one
you know was completed with the boxes filled in.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from integrations.limble import LimbleClient, LimbleConfig  # noqa: E402
from services.sync_service import APP_ENV_KEYS, load_dotenv_files  # noqa: E402
from services.wo_narrative import NARRATIVE_FIELDS, extract_narrative  # noqa: E402


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task_ids", nargs="+", help="Limble task id(s) to read, e.g. 239728.")
    parser.add_argument("--client-id", help="Limble API client id (overrides env).")
    parser.add_argument("--client-secret", help="Limble API client secret (overrides env).")
    parser.add_argument("--base-url", help="Limble API base URL.")
    parser.add_argument("--raw", action="store_true", help="Print every instruction item verbatim.")
    return parser


def _answered(item: dict) -> bool:
    """True when this item carries something a person typed or chose."""

    return any(str(item.get(key) or "").strip() for key in ("response", "answer", "value"))


def probe(client: LimbleClient, task_id: str, *, raw: bool) -> bool:
    """Report one task; return True when a full narrative came back."""

    print(f"\n=== task {task_id} " + "=" * 46)
    try:
        items = client.get_task_instructions(task_id)
    except Exception as exc:  # noqa: BLE001 - the point is to show what went wrong
        print(f"  request failed: {exc}")
        return False

    print(f"  instruction items: {len(items)}")
    if not items:
        print("  -> no instructions on this task. Try one completed after the template change.")
        return False

    # The keys actually present are the answer to "does the reader look in the
    # right place", so show them before anything is interpreted.
    keys = sorted({key for item in items for key in item})
    print(f"  keys present: {', '.join(keys)}")

    answered = [item for item in items if _answered(item)]
    print(f"  items with an answer: {len(answered)} of {len(items)}")

    if raw:
        print("  --- every item, verbatim ---")
        print(json.dumps(items, indent=2, ensure_ascii=False)[:20000])

    narrative = extract_narrative({"instructions": items})
    if not narrative:
        print("  -> NO NARRATIVE FOUND.")
        print("     The four boxes are not being recognised in this payload. Re-run with")
        print("     --raw and send that output; the field names differ from the export.")
        return False

    print("  -> narrative found:")
    for field in NARRATIVE_FIELDS:
        value = narrative.get(field.key)
        shown = value if value is not None else "(not answered)"
        if len(shown) > 90:
            shown = shown[:89] + "…"
        print(f"       {field.label:<14} {shown}")
    missing = [f.label for f in NARRATIVE_FIELDS if f.key not in narrative]
    if missing:
        print(f"     ({len(missing)} box(es) blank on this task: {', '.join(missing)})")
    return len(narrative) == len(NARRATIVE_FIELDS)


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    load_dotenv_files(only_keys=APP_ENV_KEYS)
    try:
        config = LimbleConfig.from_env(
            client_id=args.client_id,
            client_secret=args.client_secret,
            base_url=args.base_url,
        )
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    client = LimbleClient(config)
    print(f"Limble base URL: {config.base_url}")
    complete = sum(probe(client, str(task_id).strip(), raw=args.raw) for task_id in args.task_ids)

    print(f"\n{complete} of {len(args.task_ids)} task(s) returned all four boxes.")
    if complete:
        print("The reader works against the live API. A sync with --instructions will populate the tables.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
