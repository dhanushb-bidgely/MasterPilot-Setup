#!/usr/bin/env python3
"""
Post the DA-1795 MDS config pack to a pilot.

Applies the JSON files in ../DA-1795-MDS-QA-Pack/01-configs/ (01 through 05, in
order) to a pilot via POST /entities/{pilot_id}/configs - the same endpoint and
body shape the pack's 00-README documents by curl, and the same one
Persona_setup/HER/setup_her.py uses.

Each file's own per-key metadata (configDataType, configRegex,
configDocumentation) is sent as authored in the pack; this script does not
invent or override it.

Usage:
  # apply the whole pack (01-05) to the default pilot
  python post_pilot_config.py

  # preview without sending
  python post_pilot_config.py --dry-run

  # only some files
  python post_pilot_config.py --only 01 --only 03

  # a one-off key instead of the pack
  python post_pilot_config.py --config-type her_moderation --kv her_base_url=https://...

Settings resolve in this order:
  --base-url / --pilot-id / --token   flags
  BASE_URL / PILOT_ID / BIDGELY_TOKEN environment variables
  config.json next to this script     {"BASE_URL": ..., "AUTH_TOKEN": ...}
  the defaults below

Note on file 05: `dashboard.disagg_enabled_mappings` is skipped by default. The
pack authors it with an empty value and documents it as "leave unset / confirm
with Web before setting". Pass --include-unset-keys to send it anyway.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
CONFIGS_DIR = SCRIPT_DIR.parent / "DA-1795-MDS-QA-Pack" / "01-configs"

# Pilot/environment settings live in MDS_AUTOMATION/pilots.json, shared with the
# automation suite so a pilot is defined in exactly one place.
sys.path.insert(0, str(SCRIPT_DIR.parent.parent / "MDS_AUTOMATION"))
import mds_config  # noqa: E402

DEFAULT_TIMEOUT = 60

PILOT_CONFIGS_POST_PATH = "/entities/{pilot_id}/configs"

# The pack's apply order, per 00-README section 2. Ingestion/parsing configs
# must land before the field mappings and frontend configs that build on them.
PACK_FILES = (
    "01_launchpad_ingestion_configs_MDS_CORE.json",
    "02_launchpad_ingestion_configs_FILE_PARSING.json",
    "03_field_mapping_configs.json",
    "04_disagg_and_solar_configs.json",
    "05_frontend_configs_CX.json",
)

# Keys the pack ships but documents as "do not set yet". Skipped unless
# --include-unset-keys is passed.
SKIP_KEYS = {"dashboard.disagg_enabled_mappings"}

# Keys whose value the pack ties to the source pilot (73969 / test-mds). Warned
# about when applying to a different pilot, since they are almost certainly
# wrong for the target.
PILOT_SPECIFIC_KEYS = {
    "utility_name",
    "user_identifier_field_format",
    "home_identifier_field_format",
}

# Metadata defaults for --kv one-offs only. Pack keys carry their own.
DEFAULT_CONFIG_META = {
    "configDocumentation": "Set via MDS_PILOT/post-config/post_pilot_config.py",
    "configRegex": ".*",
    "configDataType": "TEXT",
}


def load_local_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        with open(CONFIG_PATH) as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return {}


# ---------------- reading the pack ---------------- #


def extract_blocks(document: dict, filename: str) -> list[dict]:
    """Normalize one pack file into a list of {configType, configKVs} blocks.

    The pack uses three shapes:
      * 01, 02: top-level configType + configKVs
      * 03, 04: a "blocks" array of {configType, configKVs}
      * 05:     top-level configType + _sectionA_verified_present and
                _sectionB_MISSING_add_these arrays (both are real keys to set;
                section B is the set the README asks QA to add)
    Keys starting with "_" are documentation, except the section arrays in 05.
    """
    if "blocks" in document:
        blocks = []
        for block in document["blocks"]:
            config_type = block.get("configType")
            kvs = block.get("configKVs") or []
            if not config_type or not kvs:
                raise ValueError(f"{filename}: a block is missing configType/configKVs")
            blocks.append({"configType": config_type, "configKVs": list(kvs)})
        return blocks

    config_type = document.get("configType")
    if not config_type:
        raise ValueError(f"{filename}: no top-level configType and no 'blocks' array")

    kvs: list[dict] = []
    if document.get("configKVs"):
        kvs.extend(document["configKVs"])
    # File 05 splits its keys across two section arrays instead of configKVs.
    for key, value in document.items():
        if key.startswith("_section") and isinstance(value, list):
            kvs.extend(value)

    if not kvs:
        raise ValueError(f"{filename}: found no config key/values to post")
    return [{"configType": config_type, "configKVs": kvs}]


def clean_kv(kv: dict) -> dict:
    """Drop documentation-only fields, keep what the API expects."""
    allowed = (
        "configKey",
        "configVal",
        "configDocumentation",
        "configRegex",
        "configDataType",
        "configTags",
    )
    cleaned = {k: v for k, v in kv.items() if k in allowed}
    # The pack omits configRegex on some section-A/B entries; the API wants one.
    cleaned.setdefault("configRegex", ".*")
    cleaned.setdefault("configDataType", "TEXT")
    return cleaned


def load_pack(only: list[str] | None, include_unset: bool) -> list[tuple[str, dict]]:
    """Return [(filename, block), ...] for the selected pack files, in order."""
    if not CONFIGS_DIR.is_dir():
        raise FileNotFoundError(f"config pack not found at {CONFIGS_DIR}")

    selected = []
    for filename in PACK_FILES:
        if only and not any(filename.startswith(prefix) for prefix in only):
            continue
        path = CONFIGS_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"{path} is missing from the pack")
        with open(path) as handle:
            document = json.load(handle)
        for block in extract_blocks(document, filename):
            kvs = []
            for kv in block["configKVs"]:
                key = kv.get("configKey")
                if not key:
                    continue
                if key in SKIP_KEYS and not include_unset:
                    print(f"  skipping {key} (pack documents it as leave-unset)")
                    continue
                kvs.append(clean_kv(kv))
            if kvs:
                selected.append((filename, {"configType": block["configType"], "configKVs": kvs}))
    if not selected:
        raise ValueError("no config files selected")
    return selected


# ---------------- posting ---------------- #


def post_block(
    base_url: str,
    auth_token: str,
    pilot_id: int,
    block: dict,
    dry_run: bool,
) -> bool:
    config_type = block["configType"]
    kvs = block["configKVs"]

    print(f"  POST configType={config_type} ({len(kvs)} key(s))")
    for kv in kvs:
        value = kv["configVal"]
        shown = value if len(str(value)) <= 70 else f"{str(value)[:67]}..."
        print(f"    {kv['configKey']} = {shown}")

    if dry_run:
        return True

    url = f"{base_url.rstrip('/')}{PILOT_CONFIGS_POST_PATH.format(pilot_id=pilot_id)}"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {auth_token}"}
    response = requests.post(
        url,
        headers=headers,
        json={"configType": config_type, "configKVs": kvs},
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code not in (200, 201):
        print(f"    FAILED: HTTP {response.status_code} - {response.text[:400]}")
        return False
    print("    ok")
    return True


def parse_kv(pairs: list[str]) -> list[dict]:
    parsed = []
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f"--kv must be KEY=VALUE, got: {pair!r}")
        key, value = pair.split("=", 1)
        if not key:
            raise ValueError(f"--kv key is empty: {pair!r}")
        parsed.append({"configKey": key, "configVal": value, **DEFAULT_CONFIG_META})
    return parsed


def warn_pilot_specific(blocks: list[tuple[str, dict]], pilot_id: int) -> None:
    found = {
        kv["configKey"]
        for _, block in blocks
        for kv in block["configKVs"]
        if kv["configKey"] in PILOT_SPECIFIC_KEYS
    }
    if not found:
        return
    print(
        f"\nNote: the pack was captured from pilot 73969 (test-mds). These keys "
        f"carry 73969's values and likely need editing for pilot {pilot_id}:"
    )
    for key in sorted(found):
        print(f"  - {key}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="PREFIX",
        help="apply only pack files starting with PREFIX (e.g. --only 01); repeatable",
    )
    parser.add_argument(
        "--include-unset-keys",
        action="store_true",
        help="also send keys the pack documents as leave-unset",
    )
    parser.add_argument("--config-type", help="one-off mode: configType to post (with --kv)")
    parser.add_argument(
        "--kv",
        action="append",
        metavar="KEY=VALUE",
        help="one-off mode: config key/value; repeatable",
    )
    parser.add_argument("--pilot-id", type=int, default=None,
                        help="pilot to configure; required unless MDS_PILOT_ID/PILOT_ID is set")
    parser.add_argument("--base-url", default=None, help="default: the pilot's environment in pilots.json")
    parser.add_argument("--token", default=None, help="else BIDGELY_TOKEN env or config.json")
    parser.add_argument("--dry-run", action="store_true", help="print requests without sending")
    args = parser.parse_args()

    local_config = load_local_config()
    pilot_id = args.pilot_id or os.environ.get("PILOT_ID") or local_config.get("PILOT_ID")
    try:
        mds_config.select(pilot_id)
        pilot_id = mds_config.pilot_id()
        base_url = (args.base_url or os.environ.get("BASE_URL")
                    or local_config.get("BASE_URL") or mds_config.get("base_url"))
    except mds_config.ConfigError as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    token = args.token or os.environ.get("BIDGELY_TOKEN") or local_config.get("AUTH_TOKEN")

    if not token and not args.dry_run:
        print(
            "No auth token. Pass --token, export BIDGELY_TOKEN, or set AUTH_TOKEN "
            "in MDS_PILOT/post-config/config.json."
        )
        return 1

    if args.config_type or args.kv:
        if not (args.config_type and args.kv):
            print("one-off mode needs both --config-type and --kv")
            return 1
        try:
            blocks = [("(--kv)", {"configType": args.config_type, "configKVs": parse_kv(args.kv)})]
        except ValueError as exc:
            print(str(exc))
            return 1
    else:
        try:
            blocks = load_pack(args.only, args.include_unset_keys)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"Could not read the config pack: {exc}")
            return 1

    print(f"\npilot   {pilot_id}")
    print(f"baseUrl {base_url}")
    if args.dry_run:
        print("mode    DRY RUN (nothing is sent)")

    failures = 0
    current_file = None
    for filename, block in blocks:
        if filename != current_file:
            print(f"\n{filename}")
            current_file = filename
        if not post_block(base_url, token or "", pilot_id, block, args.dry_run):
            failures += 1

    warn_pilot_specific(blocks, pilot_id)

    total = len(blocks)
    print(f"\n{total - failures}/{total} block(s) posted" + (" (dry run)" if args.dry_run else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
