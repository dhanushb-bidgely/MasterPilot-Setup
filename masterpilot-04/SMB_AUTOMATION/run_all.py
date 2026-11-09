#!/usr/bin/env python3
"""Run several create_smb_*_user.py scripts (88004 SMB) in parallel waves, or list them.

    python3 SMB_AUTOMATION/run_all.py list
    python3 SMB_AUTOMATION/run_all.py run welcome monthly_summary --env dev
    python3 SMB_AUTOMATION/run_all.py run all --env productqa --wave 2

Each script writes its own results/<flow>/run_<env>_<stamp>/; the console log of every script is in
results/_runner/<flow>_<env>_<stamp>.log. `--ensure-pilot-config` applies the pilot
setup once before the first wave. Exit code: 0 every script passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from smb_flows import FLOWS  # noqa: E402


def describe(flow: str) -> tuple[str, str, str]:
    d = FLOWS[flow]
    ov = dict(d["steps"])["create_user"].get("overrides") or {}
    if dict(d["steps"])["create_user"].get("dualFuel"):
        user = "Electric AMI + Gas NSM"
    else:
        user = f"{ov.get('serviceType', 'ELECTRIC').title()} {ov.get('dataStreamType', 'AMI')} {ov.get('ratePlanId', 'SMB_GS_STANDARD')}"
    return (d["email"] or {}).get("type", "- (data only)"), user, (d["email"] or {}).get("subject", "")


def run(flows: list[str], env: str, wave: int, extra: list[str]) -> int:
    logs = ROOT / "results" / "_runner"
    logs.mkdir(parents=True, exist_ok=True)
    status: dict[str, int] = {}
    for i in range(0, len(flows), wave):
        procs = []
        for f in flows[i:i + wave]:
            log = logs / f"{f}_{env}_{time.strftime('%Y%m%d_%H%M%S')}.log"
            print(f"start {f}  (log {log.relative_to(ROOT)})", flush=True)
            procs.append((f, subprocess.Popen([sys.executable, str(ROOT / f"create_smb_{f}_user.py"), "--env", env, *extra],
                                              cwd=ROOT, env={**os.environ, "PYTHONUNBUFFERED": "1"}, stdout=log.open("w"),
                                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)))
        for f, p in procs:
            status[f] = p.wait()
            print(f"done  {f}: exit {status[f]}", flush=True)
    for f, rc in status.items():
        print(f"{f:34} {rc}")
    return 0 if all(rc == 0 for rc in status.values()) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    r = sub.add_parser("run")
    r.add_argument("flows", nargs="+")
    r.add_argument("--env", default="productqa")
    r.add_argument("--wave", type=int, default=2, help="scripts run in parallel (shared queues: keep it small)")
    a, extra = ap.parse_known_args()
    if a.cmd == "list":
        print(f"{'flow (create_smb_<flow>_user.py)':34} {'email':26} {'user':36} subject")
        for f in FLOWS:
            e, u, s = describe(f)
            print(f"{f:34} {e:26} {u:36} {s}")
        return 0
    flows = list(FLOWS) if a.flows == ["all"] else a.flows
    bad = [f for f in flows if f not in FLOWS]
    if bad:
        raise SystemExit(f"unknown flow(s) {bad}; see `python3 run_all.py list`")
    if not (ROOT / "targets" / f"{a.env}.json").exists():
        raise SystemExit(f"no targets/{a.env}.json: copy targets/template.json.example")
    if "--ensure-pilot-config" in extra:       # pilot write once, before the waves; each script then preflights read-only
        extra = [x for x in extra if x != "--ensure-pilot-config"]
        r = subprocess.run([sys.executable, str(ROOT / f"create_smb_{flows[0]}_user.py"), "--env", a.env,
                            "--phases", "preflight", "--ensure-pilot-config"], cwd=ROOT)
        if r.returncode:
            return 1
    return run(flows, a.env, max(1, a.wave), extra)


if __name__ == "__main__":
    sys.exit(main())
