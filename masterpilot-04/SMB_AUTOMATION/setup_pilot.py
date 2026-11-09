#!/usr/bin/env python3
"""Set up any pilot on any env for the SMB flows, from the reference productqa 88004, with one command.

Order (each step only adds what the target lacks; nothing already set is changed):
  1. target      targets/<env>.json must exist (copy targets/template.json.example)
  2. rate plans  the plans the flows enrol users on, copied by name from the reference (band labels kept)
  3. bill cycle  the target's bill_cycle_code schedule (copy_bill_cycle.py)
  4. models      hybrid models + model-name config, plus reco model files (copy_hybrid_models.py --reco-source-env)
  5. pilot       configs, texts' prerequisites, mail settings, recommendations switch ... (sk/pilot_setup.py)
  6. check       read-only pilot check; lists what only a person can fix (mail key, servers, neighbourhoods)
Plan only (read-only) unless --apply. --set-ingestion-bucket also points the pilot's ingestion at targets
launchpad_bucket / marker_bucket (reroutes the pilot's files: only when asked).

RUN
    python3 SMB_AUTOMATION/setup_pilot.py --env productqa-88012                    # plan
    python3 SMB_AUTOMATION/setup_pilot.py --env productqa-88012 --apply
    python3 SMB_AUTOMATION/setup_pilot.py --env uat --apply --set-ingestion-bucket
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REF_ENV, REF_PILOT = "productqa", 88004
RECO_SOURCE_ENV = "uat"            # has US_88004_*_reco* files (productqa serves recommendations from CMS)


def step(n: int, title: str) -> None:
    print(f"\n===== {n}. {title}", flush=True)


def run(cmd: list[str]) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.call([sys.executable, *cmd], cwd=ROOT)


def strip_ids(o):
    if isinstance(o, dict):
        return {k: strip_ids(v) for k, v in o.items() if k not in ("id", "erateId")}
    if isinstance(o, list):
        return [strip_ids(v) for v in o]
    return o


def rate_plans(env: str, pilot: int, names: set[str], apply: bool) -> int:
    from copy_hybrid_models import Env
    ref, dst = Env(REF_ENV), Env(env)

    def plans(e: Env, p: int) -> list[dict]:
        b = e.call("GET", f"v3.0/rates/configuration/utilityId/{p}")
        res = b.get("payload", b) if isinstance(b, dict) else b
        return res if isinstance(res, list) else []
    have = {pl.get("planName"): pl for pl in plans(dst, pilot)}
    taken = {pl.get("planNumber") for pl in have.values()}
    posts = []
    for pl in plans(ref, REF_PILOT):
        name = pl.get("planName")
        if name not in names:
            continue
        if name in have:
            print(f"  {name}: already on pilot {pilot} (plan {have[name].get('planNumber')})")
            continue
        if pl.get("planNumber") in taken:
            print(f"  {name}: plan number {pl.get('planNumber')} is used by another plan on pilot {pilot}: not copied")
            continue
        q = strip_ids(pl)
        q["utilityId"] = pilot
        for c in q.get("components", []):
            c["utilityId"] = pilot
            for r in c.get("rates", []):
                r["utilityId"] = pilot
                if c.get("rateBandName"):
                    r["groupName"] = c["rateBandName"]          # the POST stores the band label from groupName
        posts.append(q)
        print(f"  {name}: {'copying' if apply else 'would copy'} plan {pl.get('planNumber')} "
              f"({sum(len(c.get('rates', [])) for c in q.get('components', []))} rates)")
    if posts and apply:
        dst.call("POST", f"v3.0/rates/configuration/utilityId/{pilot}", posts)
        now = {pl.get("planName") for pl in plans(dst, pilot)}
        missing = [q["planName"] for q in posts if q["planName"] not in now]
        print(f"  copied {len(posts)}; read back: {'all present' if not missing else 'MISSING ' + str(missing)}")
        return 1 if missing else 0
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", required=True, help="targets/<env>.json (e.g. productqa, uat, dev, productqa-88012)")
    ap.add_argument("--apply", action="store_true", help="write (default: plan only, read-only)")
    ap.add_argument("--set-ingestion-bucket", action="store_true", help="also point the pilot's ingestion buckets at the target's")
    a = ap.parse_args()

    step(1, "target")
    tf = ROOT / "targets" / f"{a.env}.json"
    if not tf.exists():
        print(f"  {tf} missing: copy targets/template.json.example to it and fill the pilot, hosts and buckets")
        return 1
    t = json.loads(tf.read_text())
    pilot = int(t["pilot"])
    os.environ["MP04_ENV"] = a.env
    sys.path.insert(0, str(ROOT))
    print(f"  {t['name']}: pilot {pilot}, {t['base_url']}, launchpad bucket {t['launchpad_bucket']}")
    if (a.env, pilot) == (REF_ENV, REF_PILOT):
        print("  this is the reference pilot: only the pilot check runs")
    rc = 0
    if (a.env, pilot) != (REF_ENV, REF_PILOT):
        from sk import pilot_setup
        step(2, "rate plans (by name, from productqa 88004)")
        rc |= rate_plans(a.env, pilot, pilot_setup.flow_plan_names(), a.apply)
        step(3, f"bill cycle {t.get('bill_cycle_code', 'SMBQA1')}")
        rc |= run(["copy_bill_cycle.py", "--target-env", a.env, "--target-pilot", str(pilot),
                   "--code", t.get("bill_cycle_code", "SMBQA1")] + (["--apply"] if a.apply else [])) not in (0,)
        step(4, "hybrid models + recommendation model files")
        rc |= run(["copy_hybrid_models.py", "--target-env", a.env, "--target-pilot", str(pilot),
                   "--reco-source-env", RECO_SOURCE_ENV] + (["--apply"] if a.apply else [])) != 0
    step(5, "pilot configs" + (" (write)" if a.apply else " (read-only)"))
    extra = (["--ensure-pilot-config"] if a.apply else []) + (["--set-ingestion-bucket"] if a.set_ingestion_bucket and a.apply else [])
    run(["create_smb_welcome_user.py", "--env", a.env, "--phases", "preflight", *extra])
    if a.apply:
        step(6, "final pilot check (read-only)")
        rc |= run(["create_smb_welcome_user.py", "--env", a.env, "--phases", "preflight"]) != 0
    print("\nNot done by this script (a person or DevOps): the mail key email_moderation.smtp_pwd, servers being up, "
          "the ingester reading a new bucket, DCX feature rows, neighbourhoods for the SBC comparison.")
    return 1 if rc else 0


if __name__ == "__main__":
    sys.exit(main())
