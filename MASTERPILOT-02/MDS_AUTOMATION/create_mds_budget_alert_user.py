#!/usr/bin/env python3
"""ONE command: create an MDS user on <deployment> the target pilot and get its Budget Alert email(s).

    1. user       mds_common creates the user (files, ingestion, disagg, aggregation); Monthly Summary held
    2. configure  user-scope subscription + home budget
    3. send       MidCycle (75%) and UsageExceed (100%) alerts, validated

RUN
    python3 mds_user_automation/create_mds_budget_alert_user.py [--alert both|midcycle|exceed]
OUTPUT  results/budget_alert/run_*/email/BA_*.html, reports/BA_SUMMARY.md
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mds_ba  # noqa: E402

if __name__ == "__main__":
    sys.exit(mds_ba.main(__doc__, "user,configure,send"))
