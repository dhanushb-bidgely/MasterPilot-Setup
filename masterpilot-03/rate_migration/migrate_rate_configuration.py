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
"""

import csv
import json
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
SOURCES_CSV_PATH = SCRIPT_DIR / "sources.csv"

TIMEOUT = 30.0


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


def fetch_rate_configuration(source_base_url, source_token, source_utility_id, plan_number=None):
    url = f"{source_base_url}/v3.0/rates/configuration/utilityId/{source_utility_id}"
    if plan_number:
        url += f"?planNumber={plan_number}"
    headers = {"Authorization": f"Bearer {source_token}"}

    response = requests.get(url, headers=headers, timeout=TIMEOUT)
    if response.status_code != 200:
        raise Exception(
            f"GET {url} failed: HTTP {response.status_code} - {response.text[:200]}"
        )

    body = response.json()
    payload = body.get("payload") if isinstance(body, dict) else body
    if payload is None:
        raise Exception(f"GET {url} returned no payload: {response.text[:200]}")
    return payload


def remap_plan_configuration(plan_configuration, target_pilot_id, target_plan_number):
    """Retarget a fetched plan configuration onto the target utility/plan, and
    strip the source-assigned rate ids so the target regenerates its own."""
    plan_configuration = dict(plan_configuration)
    plan_configuration["utilityId"] = int(target_pilot_id)
    plan_configuration["planNumber"] = int(target_plan_number)

    remapped_components = []
    for component in plan_configuration.get("components", []):
        component = dict(component)
        component["utilityId"] = int(target_pilot_id)
        component["planNumber"] = int(target_plan_number)

        remapped_rates = []
        for rate in component.get("rates", []):
            rate = dict(rate)
            rate.pop("id", None)
            rate["utilityId"] = int(target_pilot_id)
            rate["planNumber"] = int(target_plan_number)
            remapped_rates.append(rate)
        component["rates"] = remapped_rates
        remapped_components.append(component)
    plan_configuration["components"] = remapped_components

    return plan_configuration


def push_rate_configuration(target_base_url, target_token, target_pilot_id, plan_configurations):
    url = f"{target_base_url}/v3.0/rates/configuration/utilityId/{target_pilot_id}"
    headers = {"Authorization": f"Bearer {target_token}", "Content-Type": "application/json"}

    return requests.post(url, headers=headers, data=json.dumps(plan_configurations), timeout=TIMEOUT)


def process_source(row, target):
    """Migrate one CSV row. Returns True on success, False on failure."""
    source_base_url = row["SOURCE_BASE_URL"]
    source_token = row["ENV_TOKEN"]
    source_utility_id = row["SOURCE_UTILITY_ID"]
    plan_number = row["PLAN_NUMBER"].strip()
    # Optional: push under a different plan number on the target. Blank keeps
    # the source plan number.
    target_plan_number = row.get("TARGET_PLAN_NUMBER", "").strip() or plan_number

    print(
        f"\nFetching rate configuration for utility {source_utility_id}, "
        f"plan {plan_number or '(all)'} from {source_base_url}..."
    )
    try:
        payload = fetch_rate_configuration(
            source_base_url, source_token, source_utility_id, plan_number
        )
    except Exception as e:
        print(f"Failed: {e}")
        return False

    plan_configurations = payload if isinstance(payload, list) else [payload]
    print(f"Fetched {len(plan_configurations)} plan configuration(s).")

    if target_plan_number != plan_number:
        print(f"Remapping plan {plan_number} -> {target_plan_number} on target.")

    remapped = [
        remap_plan_configuration(pc, target["TARGET_PILOT_ID"], target_plan_number)
        for pc in plan_configurations
    ]

    print(
        f"Pushing rate configuration to {target['TARGET_BASE_URL']} for "
        f"utility {target['TARGET_PILOT_ID']}, plan {target_plan_number}..."
    )
    try:
        response = push_rate_configuration(
            target["TARGET_BASE_URL"],
            target["TARGET_TOKEN"],
            target["TARGET_PILOT_ID"],
            remapped,
        )
    except Exception as e:
        print(f"Failed: {e}")
        return False

    if response.status_code in (200, 201):
        print(f"Success: HTTP {response.status_code}")
        print(response.text)
        return True

    print(f"Failed: HTTP {response.status_code}")
    print(response.text)
    return False


def main():
    target = load_target_config()
    sources = load_sources()

    if not sources:
        print(f"No rows found in {SOURCES_CSV_PATH}")
        sys.exit(1)

    failures = sum(0 if process_source(row, target) else 1 for row in sources)

    if failures:
        print(f"\n{failures} of {len(sources)} row(s) failed.")
        sys.exit(1)

    print(f"\nAll {len(sources)} row(s) processed successfully.")


if __name__ == "__main__":
    main()
