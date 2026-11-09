"""The ONE place the target is described: targets/<env>.json, chosen by --env / MP04_ENV (default productqa).

Every module reads host, bucket, timezone, bill-cycle code, plan numbers/names and recipient from here; nothing about the
target is hard-coded anywhere else. New environment: copy targets/template.json.example to targets/<env>.json, edit, run with `--env <env>`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ROOT / "targets"


@dataclass(frozen=True)
class Target:
    name: str
    pilot: int
    base_url: str
    dashboard_url: str
    launchpad_bucket: str
    timezone: str
    bill_cycle_code: str
    utility_name: str
    recipient: str
    plans: dict
    segment: str = "SMB"
    debug_kit_profile: str = ""
    restart_units: dict = field(default_factory=dict)
    token: dict = field(default_factory=dict)
    aws_region: str = "us-west-2"
    program_file_prefix: str = "USERPROGRAM"    # s3_pull.s3UserProgramFilePrefix of the pilot
    marker_bucket: str = ""                      # launchpad writes Error/<key>.processed here (budget billing check)
    aggregation_priority_queue: str = ""         # SQS queue for the O9 MONTH rebuild
    sbc_donor_ntypes: tuple = ()                 # pilots whose SBC clusters are borrowed when the target has none
    hybrid_model_bucket: str = ""                # default hybrid-models-<env>; models at masterpilot/US_<pilot>_*
    # reference the email texts are read from at run time (strings step); any env/pilot with a targets/<env>.json
    string_source: dict = field(default_factory=lambda: {"env": "productqa", "pilot": 88004})

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def plan(self, kind: str) -> dict:
        return self.plans[kind]


def load(name: str | None = None) -> Target:
    name = name or os.environ.get("MP04_ENV") or os.environ.get("SMB_TARGET", "productqa")
    p = Path(name) if name.endswith(".json") else TARGETS / f"{name}.json"
    if not p.exists():
        raise SystemExit(f"target file {p} not found: copy targets/template.json.example to targets/<env>.json and pass --env <env>")
    d = json.loads(p.read_text())
    known = set(Target.__dataclass_fields__)
    if "sbc_donor_ntypes" in d:
        d["sbc_donor_ntypes"] = tuple(d["sbc_donor_ntypes"])
    return Target(**{k: v for k, v in d.items() if k in known})


T = load()
