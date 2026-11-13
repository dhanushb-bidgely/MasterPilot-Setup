#!/usr/bin/env python3
"""Test SMB Monthly Summary Email (Gas NSM) for a Gas NSM only OPT OUT user on SMB_GAS_STANDARD.

User   new SMB launchpad user: Gas NSM, plan SMB_GAS_STANDARD
Data   no interval data + generated invoice
Email  MONTHLY_SUMMARY, subject contains 'on gas in the last billing cycle'

Every run builds new launchpad files with unique ids and data anchored to today (seed RAW shifted so the last reading is
yesterday, invoice priced on the user's own completed billing cycles), uploads them, writes the user-scope configs and
strings of this email, triggers the platform and waits for the email. Checks: C01 user/meters/plan, C02 RAW ingested,
C03 invoices = generated, E01 email stored, E02 subject, E03 rendered, E04 every configured section, M01 bill amount = billing_cost of the last cycle, M02 period, M03 demand / M04 budget billing / M05 TOU when the plan has them.

RUN
    python3 SMB_AUTOMATION/create_smb_monthly_summary_gas_nsm_no_itemization_gas_chart_user.py                         # productqa (default)
    python3 SMB_AUTOMATION/create_smb_monthly_summary_gas_nsm_no_itemization_gas_chart_user.py --env dev               # any targets/<env>.json
    python3 SMB_AUTOMATION/create_smb_monthly_summary_gas_nsm_no_itemization_gas_chart_user.py --email you@bidgely.com
    python3 SMB_AUTOMATION/create_smb_monthly_summary_gas_nsm_no_itemization_gas_chart_user.py --manifest SMB_AUTOMATION/results/monthly_summary_gas_nsm_no_itemization_gas_chart/run_<env>_<stamp>/manifest.json

OUTPUT  SMB_AUTOMATION/results/monthly_summary_gas_nsm_no_itemization_gas_chart/run_<env>_<stamp>/
    SUMMARY.txt              user details, uploads, email, check table
    manifest.json            run variables + uploads (used by --manifest to re-check the same user)
    input_files/             the launchpad files uploaded to S3
    logs/run.log             full timestamped log
    reports/                 validation_report.md / .json
    email/                   <TYPE>.html exactly as stored
    api_evidence/            raw API responses behind the checks
Exit code: 0 = all checks PASS/WARN, 1 = a check FAILed or a step broke, 130 = interrupted.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sk.boot import bootstrap  # noqa: E402

ARGV = bootstrap()
import smb_common  # noqa: E402

if __name__ == "__main__":
    sys.exit(smb_common.main(__doc__, "monthly_summary_gas_nsm_no_itemization_gas_chart", ARGV))
