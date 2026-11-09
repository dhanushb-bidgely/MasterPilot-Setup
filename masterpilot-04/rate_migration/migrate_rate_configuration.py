#!/usr/bin/env python3
"""
Fetch utility rate plan configurations from one or more source environments
(listed in a CSV) and push each into a single target environment (configured
via JSON).

Unlike rate_structure_migration/, this uses the GET
/v3.0/rates/configuration/utilityId/{id}[?planNumber=] endpoint, which already
returns the same nested shape the POST /v3.0/rates/configuration/utilityId/{id}
endpoint expects - so each plan is remapped onto the target utility/plan
numbers and posted back as-is, with no flat-to-nested reshaping needed.

Two things the POST needs that the GET does not hand back as-is:
  - every source id is removed (the target assigns its own), and
  - each rate's groupName is set from its component's rateBandName: the POST
    stores the band label ("Customer Charge", "Winter", ...) from
    rates[].groupName, so without it the target plan loses its labels.

A plan that is already identical on the target is skipped. Before a plan is
replaced its current target version is saved to backups/, and every pushed
plan is read back and compared with the source.

Usage:
  python migrate_rate_configuration.py --dry-run   # show what would be pushed
  python migrate_rate_configuration.py             # push, back up, read back
"""

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SOURCES_CSV_PATH = SCRIPT_DIR / "sources.csv"
BACKUP_DIR = SCRIPT_DIR / "backups"

TIMEOUT = 30.0

# Fields the platform fills in per environment; ignored when comparing plans.
VOLATILE_KEYS = {"id", "erateId", "utilityId", "mappedUtility", "sourceFile"}


def load_target_config():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    for key in ("TARGET_BASE_URL", "TARGET_TOKEN", "TARGET_PILOT_ID"):
        if not config.get(key):
            raise ValueError(f"Missing '{key}' in {CONFIG_PATH}")
    return config


def load_sources():
    with open(SOURCES_CSV_PATH, newline="") as f:
        rows = list(csv.DictReader(f))

    required = {"SOURCE_BASE_URL", "ENV_TOKEN", "SOURCE_UTILITY_ID", "PLAN_NUMBER"}
    last = {"SOURCE_BASE_URL": None, "ENV_TOKEN": None}
    for i, row in enumerate(rows, start=2):
        missing = required - row.keys()
        if missing:
            raise ValueError(f"{SOURCES_CSV_PATH} row {i} missing columns: {missing}")

        # Allow SOURCE_BASE_URL/ENV_TOKEN to be left blank on rows that share
        # the same source env as the row above them.
        for key in ("SOURCE_BASE_URL", "ENV_TOKEN"):
            if row[key].strip():
                last[key] = row[key].strip()
            elif last[key] is not None:
                row[key] = last[key]
            else:
                raise ValueError(
                    f"{SOURCES_CSV_PATH} row {i}: '{key}' is blank with no prior "
                    "value to fill down from"
                )
    return rows


def fetch_rate_configuration(base_url, token, utility_id, plan_number=None, allow_missing=False):
    url = f"{base_url}/v3.0/rates/configuration/utilityId/{utility_id}"
    if plan_number:
        url += f"?planNumber={plan_number}"
    headers = {"Authorization": f"Bearer {token}"}

    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    if allow_missing and response.status_code in (400, 404):
        return []
    if response.status_code != 200:
        raise Exception(
            f"GET {url} failed: HTTP {response.status_code} - {response.text[:200]}"
        )

    body = response.json()
    payload = body.get("payload") if isinstance(body, dict) else body
    if payload is None:
        if allow_missing:
            return []
        raise Exception(f"GET {url} returned no payload: {response.text[:200]}")
    return payload if isinstance(payload, list) else [payload]


def strip_ids(value):
    if isinstance(value, dict):
        return {k: strip_ids(v) for k, v in value.items() if k not in ("id", "erateId")}
    if isinstance(value, list):
        return [strip_ids(v) for v in value]
    return value


def comparable(plan_configuration):
    """Plan as JSON without the per-environment fields, for an identical-or-not check."""
    def clean(value):
        if isinstance(value, dict):
            return {k: clean(v) for k, v in value.items() if k not in VOLATILE_KEYS}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value
    return json.dumps(clean(plan_configuration), sort_keys=True)


def remap_plan_configuration(plan_configuration, target_pilot_id, target_plan_number):
    """Retarget a fetched plan configuration onto the target utility/plan, strip
    every source-assigned id so the target regenerates its own, and carry each
    component's rateBandName into its rates' groupName (the label the POST stores)."""
    plan_configuration = strip_ids(plan_configuration)
    plan_configuration["utilityId"] = int(target_pilot_id)
    plan_configuration["planNumber"] = int(target_plan_number)

    for component in plan_configuration.get("components", []):
        component["utilityId"] = int(target_pilot_id)
        component["planNumber"] = int(target_plan_number)
        for rate in component.get("rates", []):
            rate["utilityId"] = int(target_pilot_id)
            rate["planNumber"] = int(target_plan_number)
            if component.get("rateBandName"):
                rate["groupName"] = component["rateBandName"]

    return plan_configuration


def push_rate_configuration(target_base_url, target_token, target_pilot_id, plan_configurations):
    url = f"{target_base_url}/v3.0/rates/configuration/utilityId/{target_pilot_id}"
    headers = {"Authorization": f"Bearer {target_token}", "Content-Type": "application/json"}

    return requests.post(url, headers=headers, data=json.dumps(plan_configurations), timeout=TIMEOUT)


def process_source(row, target, dry_run):
    """Migrate one CSV row. Returns True on success, False on failure."""
    source_base_url = row["SOURCE_BASE_URL"]
    source_token = row["ENV_TOKEN"]
    source_utility_id = row["SOURCE_UTILITY_ID"]
    plan_number = row["PLAN_NUMBER"].strip()
    # Optional: push under a different plan number on the target. Blank keeps
    # the source plan number (only allowed when PLAN_NUMBER names one plan).
    target_plan_number = row.get("TARGET_PLAN_NUMBER", "").strip()
    if target_plan_number and not plan_number:
        print("Failed: TARGET_PLAN_NUMBER needs a PLAN_NUMBER on the same row")
        return False

    print(
        f"\nFetching rate configuration for utility {source_utility_id}, "
        f"plan {plan_number or '(all)'} from {source_base_url}..."
    )
    try:
        plan_configurations = fetch_rate_configuration(
            source_base_url, source_token, source_utility_id, plan_number
        )
    except Exception as e:
        print(f"Failed: {e}")
        return False
    print(f"Fetched {len(plan_configurations)} plan configuration(s).")

    target_id, target_url, target_token = target["TARGET_PILOT_ID"], target["TARGET_BASE_URL"], target["TARGET_TOKEN"]
    ok = True
    for pc in plan_configurations:
        number = target_plan_number or pc.get("planNumber")
        if str(number) != str(pc.get("planNumber")):
            print(f"Remapping plan {pc.get('planNumber')} -> {number} on target.")
        remapped = remap_plan_configuration(pc, target_id, number)
        expected = comparable(dict(pc, planNumber=int(number)))     # what the target should read back
        try:
            current = fetch_rate_configuration(target_url, target_token, target_id, number, allow_missing=True)
        except Exception as e:
            print(f"Failed: {e}")
            ok = False
            continue
        if current and comparable(current[0]) == expected:
            print(f"Plan {number} {pc.get('planName')}: already identical on target, skipped.")
            continue
        action = "replace" if current else "create"
        if dry_run:
            print(f"Plan {number} {pc.get('planName')}: would {action} "
                  f"({sum(len(c.get('rates', [])) for c in remapped.get('components', []))} rates).")
            continue
        if current:
            BACKUP_DIR.mkdir(exist_ok=True)
            backup = BACKUP_DIR / f"utility{target_id}_plan{number}_{datetime.now():%Y%m%d_%H%M%S}.json"
            backup.write_text(json.dumps(current, indent=1))
            print(f"Backed up target plan {number} -> {backup}")
        print(f"Pushing plan {number} {pc.get('planName')} to {target_url} for utility {target_id} ({action})...")
        try:
            response = push_rate_configuration(target_url, target_token, target_id, [remapped])
        except Exception as e:
            print(f"Failed: {e}")
            ok = False
            continue
        if response.status_code not in (200, 201):
            print(f"Failed: HTTP {response.status_code}")
            print(response.text)
            ok = False
            continue
        after = fetch_rate_configuration(target_url, target_token, target_id, number, allow_missing=True)
        if after and comparable(after[0]) == expected:
            print(f"Success: HTTP {response.status_code}, read back identical to the source.")
        else:
            print(f"Failed: HTTP {response.status_code} but the read-back differs from the source.")
            ok = False
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="fetch and compare only; push nothing")
    args = parser.parse_args()

    target = load_target_config()
    sources = load_sources()

    if not sources:
        print(f"No rows found in {SOURCES_CSV_PATH}")
        sys.exit(1)

    failures = sum(0 if process_source(row, target, args.dry_run) else 1 for row in sources)

    if failures:
        print(f"\n{failures} of {len(sources)} row(s) failed.")
        sys.exit(1)

    print(f"\nAll {len(sources)} row(s) processed successfully" + (" (dry run)." if args.dry_run else "."))


if __name__ == "__main__":
    main()
