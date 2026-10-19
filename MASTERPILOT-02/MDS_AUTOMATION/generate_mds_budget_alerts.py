#!/usr/bin/env python3
"""Generate the 75% (MidCycleBudgetAlert) and 100% (UsageExceedBudgetAlert) Budget Alert emails for a NEW MDS user on
target pilot the target pilot, with the days remaining you choose, and validate them.

    python3 mds_user_automation/generate_mds_budget_alerts.py --days-left 6 --days-over 1

    --days-left N    "N Days Left in Billing Cycle" in both emails. The generated data ends so that
                     cycle last day - last data day = N (reachable range is printed if N is not).
    --days-over D    the 100% budget was crossed D days before the alert (default 0 = on the last data day; the
                     platform only alerts within max_threshold_days_for_budget_alert=2 days, so D>1 widens that
                     window at USER scope). The email has NO "days over" text: the event carries only daysLeft,
                     so D is verified from the daily cost (check X15), not from the email.
    --alert both|midcycle|exceed     which emails (default both)
    --uuid <u>       existing MDS user instead (its own data decides days left)

The user's data must cover >= 3 days of the open calendar-month cycle: run from the 4th of the month.
OUTPUT  results/budget_alert/run_*/email/BA_*.html, reports/BA_SUMMARY.md (36+ checks). See mds_ba.py for the gate chain.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_ba  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_ba.main(__doc__, "user,configure,send"))
