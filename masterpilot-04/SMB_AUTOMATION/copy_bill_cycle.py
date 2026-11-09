#!/usr/bin/env python3
"""Copy a bill-cycle code's schedule (2.1/utilityBillingCycles rows) from one pilot to another, any env to any env.

The SMB flows enrol every user on the target's bill_cycle_code (SMBQA1); a pilot without those rows cannot bill them.
Rows are created on the target only when the target has none for that code: an existing schedule is never changed
(a differing one is reported, not touched). Read-only unless --apply; the source rows are saved to
results/bill_cycle_copy/<env>_<pilot>_<stamp>/ and the target schedule is read back after the copy.

RUN
    python3 SMB_AUTOMATION/copy_bill_cycle.py --target-env productqa --target-pilot 88012                 # plan only
    python3 SMB_AUTOMATION/copy_bill_cycle.py --target-env productqa --target-pilot 88012 --apply
    python3 SMB_AUTOMATION/copy_bill_cycle.py --target-env uat --target-pilot 88004 --code SMBQA1 --source-pilot 88004
Tokens: as copy_hybrid_models.py (targets/<env>.json "token.env", from the environment or SMB_AUTOMATION/.env).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from copy_hybrid_models import Env  # noqa: E402

FIELDS = ("validFrom", "validTo", "fromMonth", "toMonth", "fromDay", "toDay")


def rows(env: Env, pilot: int, code: str) -> list[dict]:
    try:
        body = env.call("GET", f"2.1/utilityBillingCycles/utility/{pilot}/identifier/{code}")
    except SystemExit:                                   # 400/404: no rows for that code
        return []
    p = body.get("payload", body) if isinstance(body, dict) else body
    p = p.get("data", []) if isinstance(p, dict) else p
    return [r for r in p or [] if str(r.get("billCycleCode")) == code]


def schedule(rs: list[dict]) -> list[tuple]:
    return sorted(tuple(int(r[f]) for f in FIELDS) for r in rs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-env", default="productqa")
    ap.add_argument("--source-pilot", type=int, default=88004)
    ap.add_argument("--target-env", required=True)
    ap.add_argument("--target-pilot", type=int, required=True)
    ap.add_argument("--code", default="SMBQA1", help="bill-cycle code (targets bill_cycle_code)")
    ap.add_argument("--utility-name", help="utilityName on the new rows (default: the target pilot's name)")
    ap.add_argument("--apply", action="store_true", help="create the rows (default: plan only)")
    a = ap.parse_args()
    if (a.source_env, a.source_pilot) == (a.target_env, a.target_pilot):
        raise SystemExit("source and target are the same pilot on the same environment")
    src, dst = Env(a.source_env), Env(a.target_env)
    have_src, have_dst = rows(src, a.source_pilot, a.code), rows(dst, a.target_pilot, a.code)
    print(f"{a.code}: source {src.name} pilot {a.source_pilot} has {len(have_src)} row(s); "
          f"target {dst.name} pilot {a.target_pilot} has {len(have_dst)}  ({'APPLY' if a.apply else 'plan only'})")
    if not have_src:
        raise SystemExit(f"no {a.code} rows on the source pilot")
    if have_dst:
        same = schedule(have_dst) == schedule(have_src)
        print(f"target already has {a.code}: {'identical schedule, nothing to do' if same else 'DIFFERENT schedule, left as it is'}")
        return 0 if same else 1
    name = a.utility_name or (dst.call("GET", f"v2.0/utilities/{a.target_pilot}").get("payload") or {}).get("name") or ""
    if not a.apply:
        print(f"would create {len(have_src)} {a.code} row(s) on pilot {a.target_pilot} (utilityName {name!r}); re-run with --apply")
        return 0
    out = ROOT / "results" / "bill_cycle_copy" / f"{dst.name}_{a.target_pilot}_{datetime.now():%Y%m%d_%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"source_{a.source_pilot}_{a.code}.json").write_text(json.dumps(have_src, indent=1))
    for r in have_src:
        dst.call("POST", "2.1/utilityBillingCycles", {"billCycleCode": a.code, "utilityID": str(a.target_pilot),
                                                      "utilityName": name, **{f: str(r[f]) for f in FIELDS}})
    now = rows(dst, a.target_pilot, a.code)
    ok = schedule(now) == schedule(have_src)
    print(f"created {len(have_src)} row(s); target now has {len(now)}, schedule identical to the source: {ok}; source rows in {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
