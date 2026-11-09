#!/usr/bin/env python3
"""Test SMB Small Business Comparison (SBC, NEIGHBOURHOOD_COMPARISON) combined dual-fuel Email for an Electric AMI + Gas NSM user.

User   new SMB launchpad user: Electric AMI (SMB_GS_STANDARD) + Gas NSM (SMB_GAS_STANDARD)
Data   historicalData_SMB_AMI.csv + generated invoice
Email  NEIGHBOURHOOD_COMPARISON, subject contains 'how your electricity and gas usage compares to similar homes'

Every run builds new launchpad files with unique ids and data anchored to today (seed RAW shifted so the last reading is
yesterday, invoice priced on the user's own completed billing cycles), uploads them, writes the user-scope configs and
strings of this email, triggers the platform and waits for the email. Checks: C01 user/meters/plan, C02 RAW ingested,
C03 invoices = generated, E01 email stored, E02 subject, E03 rendered, E04 every configured section, N01 comparison amounts, N02 both fuels.

RUN
    python3 SMB_AUTOMATION/create_smb_sbc_dual_fuel_user.py                         # productqa (default)
    python3 SMB_AUTOMATION/create_smb_sbc_dual_fuel_user.py --env dev               # any targets/<env>.json
    python3 SMB_AUTOMATION/create_smb_sbc_dual_fuel_user.py --email you@bidgely.com
    python3 SMB_AUTOMATION/create_smb_sbc_dual_fuel_user.py --manifest SMB_AUTOMATION/results/sbc_dual_fuel/run_<env>_<stamp>/manifest.json

OUTPUT  SMB_AUTOMATION/results/sbc_dual_fuel/run_<env>_<stamp>/
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
    sys.exit(smb_common.main(__doc__, "sbc_dual_fuel", ARGV))
