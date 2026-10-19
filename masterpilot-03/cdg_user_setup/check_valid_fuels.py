#!/usr/bin/env python3
"""
Look up valid meter fuels (DETO /v1/valid-meter-fuel/{source_env}) for every
UUID in an input CSV, and write a per-user fuel listing plus a summary count.

Usage:
    python3 check_valid_fuels.py <input_csv> [--source-env NA2] [--out OUT.csv]

Input CSV must have a "uuid" column (as exported by the query_result_*.csv files).
"""

import argparse
import csv
import json
import sys
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

BATCH_SIZE = 100
TIMEOUT = 60.0


def load_token():
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    base_url = config["DETO_BASE_URL"]
    token = config["DETO_ACCESS_TOKEN"].strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer ") :].strip()
    return base_url, token


def load_uuids(csv_path):
    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows or "uuid" not in rows[0]:
        raise ValueError(f"{csv_path} has no 'uuid' column")
    return [row["uuid"].strip() for row in rows if row["uuid"].strip()]


def fetch_valid_fuels(base_url, token, source_env, uuids):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
    }
    results = []
    for i in range(0, len(uuids), BATCH_SIZE):
        batch = uuids[i : i + BATCH_SIZE]
        print(f"Checking fuels for {len(batch)} UUID(s) ({i + 1}-{i + len(batch)} of {len(uuids)})...")
        response = requests.post(
            f"{base_url}/v1/valid-meter-fuel/{source_env}",
            headers=headers,
            json={"source_uuids": batch},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        results.extend(body.get("results", []))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_csv", help="CSV with a 'uuid' column")
    parser.add_argument("--source-env", default="NA2", help="Source environment (default NA2)")
    parser.add_argument("--out", help="Output CSV path (default: <input>_fuels.csv next to input)")
    args = parser.parse_args()

    input_path = Path(args.input_csv)
    out_path = Path(args.out) if args.out else input_path.with_name(input_path.stem + "_fuels.csv")

    uuids = load_uuids(input_path)
    print(f"Loaded {len(uuids)} UUID(s) from {input_path}")

    base_url, token = load_token()
    results_by_uuid = {}
    for r in fetch_valid_fuels(base_url, token, args.source_env, uuids):
        results_by_uuid[r["source_uuid"]] = r

    fuel_counts = {}
    nsm_water_count = 0
    ami_electric_count = 0
    either_count = 0

    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["uuid", "status", "valid_meter_fuels", "has_NSM-WATER", "has_AMI-ELECTRIC"])
        for uuid in uuids:
            r = results_by_uuid.get(uuid)
            status = r.get("status") if r else "not_found"
            fuels = r.get("valid_meter_fuels") or [] if r else []
            has_water = "NSM-WATER" in fuels
            has_elec = "AMI-ELECTRIC" in fuels
            if has_water:
                nsm_water_count += 1
            if has_elec:
                ami_electric_count += 1
            if has_water or has_elec:
                either_count += 1
            for fuel in fuels:
                fuel_counts[fuel] = fuel_counts.get(fuel, 0) + 1
            writer.writerow([uuid, status, "|".join(fuels), has_water, has_elec])

    print(f"\nWrote {out_path}")
    print(f"\nTotal UUIDs checked: {len(uuids)}")
    print(f"Found in DETO: {len(results_by_uuid)}")
    print(f"\nFuel code counts across all users:")
    for fuel, count in sorted(fuel_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {fuel}: {count}")
    print(f"\nNSM-WATER: {nsm_water_count}")
    print(f"AMI-ELECTRIC: {ami_electric_count}")
    print(f"Either NSM-WATER or AMI-ELECTRIC: {either_count}")


if __name__ == "__main__":
    main()
