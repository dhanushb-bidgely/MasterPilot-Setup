#!/usr/bin/env python3
"""Give any pilot the hybrid models of productqa 88004, named for that pilot, and the config that names them.

What 88004 on productqa has (2026-10-29 SMB model swap):
  S3     hybrid-models-<env>/masterpilot/US_88004_{AMI,AMR,NSM}_{electric,gas}* (39 files; electric = Avista SMB 10047)
  config hybrid_moderation.csv_filename_format = Home.country-User.pilotId-UserType.meterType: the pilot looks for
         masterpilot/US_<pilot>_<meterType>_<fuel>*.

For the target this script
  1. copies every source model file to masterpilot/US_<target pilot>_..., renamed only (contents byte-identical);
     a target file that differs is first kept as <key>.superseded-<epoch> (copy it back to undo);
  2. sets the target pilot's hybrid_moderation.csv_filename_format to that same format when it differs (backed up
     first), so the target loads the copied files. No other config is touched.
Read-only unless --apply. Backups and the plan go to results/hybrid_copy/<env>_<pilot>_<stamp>/.

RUN
    python3 SMB_AUTOMATION/copy_hybrid_models.py --target-env uat --target-pilot 88002            # plan only
    python3 SMB_AUTOMATION/copy_hybrid_models.py --target-env uat --target-pilot 88002 --apply
    python3 SMB_AUTOMATION/copy_hybrid_models.py --target-env dev --target-pilot 88004 --source-env productqa --source-pilot 88004
Tokens: targets/<env>.json "token.env" (MASTERPILOT_TOKEN / MASTERPILOT_UAT_TOKEN / MASTERPILOT_DEV_TOKEN) from the
environment or SMB_AUTOMATION/.env. AWS credentials need s3 read on the source bucket and read/write on the target one.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent
PREFIX = "masterpilot/US_{pilot}_"
HYBRID_KEYS = {"hybrid_moderation": ["csv_filename_format"]}       # model file name = US_<pilot>_<meterType>_<fuel>


class Env:
    """One environment's API (base URL + token from targets/<env>.json) and its hybrid-models bucket."""

    def __init__(self, name: str):
        t = json.loads((ROOT / "targets" / f"{name}.json").read_text())
        self.name, self.base = name, t["base_url"].rstrip("/")
        self.bucket = t.get("hybrid_model_bucket") or f"hybrid-models-{name}"
        self.region = t.get("aws_region", "us-west-2")
        var = t.get("token", {}).get("env", "MASTERPILOT_TOKEN")
        env_file = ROOT / t.get("token", {}).get("env_file", ".env")
        if not os.environ.get(var) and env_file.is_file():
            for line in env_file.read_text().splitlines():
                if line.strip().startswith(f"{var}="):
                    os.environ[var] = line.split("=", 1)[1].strip().strip('"').strip("'")
        if not os.environ.get(var):
            raise SystemExit(f"no token for {name}: export {var} or put it in {env_file}")
        self.token = os.environ[var].strip().removeprefix("Bearer ")

    def call(self, method: str, path: str, body=None):
        req = urllib.request.Request(f"{self.base}/{path.lstrip('/')}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                text = r.read().decode()
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{self.name} {method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
        return json.loads(text) if text else {}

    def config(self, pilot: int, ctype: str) -> dict[str, dict]:
        body = self.call("GET", f"entities/pilot/{pilot}/configs/{ctype}")
        res = body.get("payload", body) if isinstance(body, dict) else {}
        try:
            return {kv["key"]: kv for kv in (json.loads((res or {}).get(ctype) or "{}") or {}).get("kvs") or []}
        except (TypeError, ValueError):
            return {}


def models(s3, bucket: str, pilot: int, reco: bool = False) -> dict[str, str]:
    """{suffix after US_<pilot>_: ETag} for the live model files (superseded backups excluded): the hybrid models, or
    with reco=True the recommendation model files (*_reco*) a pilot with reco_cms_enabled=false reads."""
    out, pre = {}, PREFIX.format(pilot=pilot)
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=pre):
        for o in page.get("Contents", []):
            k = o["Key"][len(pre):]
            if ".superseded-" not in o["Key"] and ("_reco" in k) == reco:
                out[k] = o["ETag"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-env", default="productqa")
    ap.add_argument("--source-pilot", type=int, default=88004)
    ap.add_argument("--target-env", required=True)
    ap.add_argument("--target-pilot", type=int, required=True)
    ap.add_argument("--reco-source-env", help="also copy the recommendation model files (*_reco*) from this env's bucket "
                    "(productqa 88004 serves recommendations from CMS and has none; uat 88004 has them)")
    ap.add_argument("--reco-source-pilot", type=int, default=88004)
    ap.add_argument("--apply", action="store_true", help="write S3 files and pilot configs (default: plan only)")
    a = ap.parse_args()
    if (a.source_env, a.source_pilot) == (a.target_env, a.target_pilot):
        raise SystemExit("source and target are the same pilot on the same environment")
    src, dst = Env(a.source_env), Env(a.target_env)
    s3 = boto3.client("s3", region_name=dst.region)
    out = ROOT / "results" / "hybrid_copy" / f"{dst.name}_{a.target_pilot}_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"source {src.name} pilot {a.source_pilot} -> target {dst.name} pilot {a.target_pilot}  ({'APPLY' if a.apply else 'plan only'})")

    # ---- model files
    have_src, have_dst = models(s3, src.bucket, a.source_pilot), models(s3, dst.bucket, a.target_pilot)
    if not have_src:
        raise SystemExit(f"no model files under s3://{src.bucket}/{PREFIX.format(pilot=a.source_pilot)}")
    copy = {k: e for k, e in have_src.items() if have_dst.get(k) != e}
    replace = sorted(k for k in copy if k in have_dst)
    extra = sorted(set(have_dst) - set(have_src))
    print(f"models: {len(have_src)} in source, {len(have_src) - len(copy)} already identical on target, "
          f"{len(copy) - len(replace)} new, {len(replace)} to replace (kept as .superseded-<epoch>)")
    if extra:
        print(f"  target-only files left as they are: {extra}")

    # ---- recommendation model files (optional)
    reco_copy, reco_src = {}, None
    if a.reco_source_env:
        reco_src = Env(a.reco_source_env)
        have_r, dst_r = models(s3, reco_src.bucket, a.reco_source_pilot, True), models(s3, dst.bucket, a.target_pilot, True)
        reco_copy = {k: e for k, e in have_r.items() if dst_r.get(k) != e}
        print(f"reco models: {len(have_r)} in {reco_src.bucket} pilot {a.reco_source_pilot}, {len(reco_copy)} to copy")

    # ---- configs
    writes: dict[str, dict[str, str]] = {}
    for ctype, keys in HYBRID_KEYS.items():
        s, d = src.config(a.source_pilot, ctype), dst.config(a.target_pilot, ctype)
        for k in keys:
            v = (s.get(k) or {}).get("val")
            if v in (None, ""):
                print(f"  {ctype}.{k}: source has no value, skipped")
            elif (d.get(k) or {}).get("val") != v or (d.get(k) or {}).get("configSource") != "PILOT":
                writes.setdefault(ctype, {})[k] = v
                print(f"  {ctype}.{k}: {(d.get(k) or {}).get('val')!r:.50} [{(d.get(k) or {}).get('configSource')}] -> {v!r:.60}")
    print(f"configs: {sum(len(v) for v in writes.values())} key(s) to write in {sorted(writes) or 'none'}")
    (out / "plan.json").write_text(json.dumps({"copy": sorted(copy), "replace": replace, "configs": writes}, indent=1))
    if not a.apply:
        print(f"plan only; re-run with --apply. Plan: {out / 'plan.json'}")
        return 0

    # ---- apply
    stamp = int(time.time())
    for k in sorted(copy):
        key = PREFIX.format(pilot=a.target_pilot) + k
        if k in have_dst:
            s3.copy_object(Bucket=dst.bucket, Key=f"{key}.superseded-{stamp}", CopySource={"Bucket": dst.bucket, "Key": key})
        s3.copy_object(Bucket=dst.bucket, Key=key,
                       CopySource={"Bucket": src.bucket, "Key": PREFIX.format(pilot=a.source_pilot) + k})
    for k in sorted(reco_copy):
        s3.copy_object(Bucket=dst.bucket, Key=PREFIX.format(pilot=a.target_pilot) + k,
                       CopySource={"Bucket": reco_src.bucket, "Key": PREFIX.format(pilot=a.reco_source_pilot) + k})
    if reco_copy:
        print(f"copied {len(reco_copy)} reco model file(s) from {reco_src.bucket}")
    for ctype, kv in writes.items():
        (out / f"BACKUP_pilot{a.target_pilot}_{ctype}.json").write_text(
            json.dumps(dst.call("GET", f"entities/pilot/{a.target_pilot}/configs/{ctype}"), indent=2))
        dst.call("POST", f"entities/{a.target_pilot}/configs", {"configType": ctype, "configKVs": [
            {"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
             "configDocumentation": f"{ctype}.{k} copied from {src.name} {a.source_pilot} by copy_hybrid_models.py"}
            for k, v in kv.items()]})
    print(f"applied: {len(copy)} file(s) copied ({len(replace)} replaced, backups .superseded-{stamp}), "
          f"{sum(len(v) for v in writes.values())} config key(s); backups in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
