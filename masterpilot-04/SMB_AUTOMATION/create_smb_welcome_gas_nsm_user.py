#!/usr/bin/env python3
"""Test SMB Welcome Email for a gas-only Gas NSM OPT OUT user on SMB_GAS_STANDARD.

User   new SMB launchpad user: Gas NSM, plan SMB_GAS_STANDARD
Data   no interval data + generated invoice
Email  USER_WELCOME, subject contains 'Welcome to your new alerts from Energy Co!'

Every run builds new launchpad files with unique ids and data anchored to today (seed RAW shifted so the last reading is
yesterday, invoice priced on the user's own completed billing cycles), uploads them, writes the user-scope configs and
strings of this email, triggers the platform and waits for the email. Checks: C01 user/meters/plan, C02 RAW ingested,
C03 invoices = generated, E01 email stored, E02 subject, E03 rendered, E04 every configured section, W01 greeting with the account name, W02 portal link.

RUN
    python3 SMB_AUTOMATION/create_smb_welcome_gas_nsm_user.py                         # productqa (default)
    python3 SMB_AUTOMATION/create_smb_welcome_gas_nsm_user.py --env dev               # any targets/<env>.json
    python3 SMB_AUTOMATION/create_smb_welcome_gas_nsm_user.py --email you@bidgely.com
    python3 SMB_AUTOMATION/create_smb_welcome_gas_nsm_user.py --manifest SMB_AUTOMATION/results/welcome_gas_nsm/run_<env>_<stamp>/manifest.json

OUTPUT  SMB_AUTOMATION/results/welcome_gas_nsm/run_<env>_<stamp>/
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
    sys.exit(smb_common.main(__doc__, "welcome_gas_nsm", ARGV))
