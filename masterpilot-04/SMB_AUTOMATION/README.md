# SMB_AUTOMATION: pilot 88004 (SMB) users and emails

The MASTERPILOT-02 `MDS_AUTOMATION` pattern for the SMB pilot. There is one `create_smb_<flow>_user.py` per email. Each creates a
brand-new SMB launchpad user, loads its data, writes that email's user-scope configs and strings, waits for the email
and validates it against the platform's own APIs. Everything it needs is in this folder: flows in `smb_flows.py`, seed
files in `data/`, the engine in `smb_common.py`.

## Setup

1. Python 3.12 with `boto3`, and AWS credentials that can `s3:PutObject` to the target's `launchpad_bucket`.
2. Token: put it in `SMB_AUTOMATION/.env` (git-ignored, chmod 600) under the name in `targets/<env>.json` `token.env`
   (`MASTERPILOT_TOKEN`, `MASTERPILOT_UAT_TOKEN`, `MASTERPILOT_DEV_TOKEN`), or export it.
3. Environment: `targets/<env>.json` (productqa, uat, dev). For another env, copy `targets/template.json.example`.

## Run

```bash
python3 SMB_AUTOMATION/create_smb_welcome_user.py --env dev
python3 SMB_AUTOMATION/create_smb_monthly_summary_user.py                     # productqa
python3 SMB_AUTOMATION/create_smb_budget_alert_user.py --manifest SMB_AUTOMATION/results/budget_alert/run_<env>_<stamp>/manifest.json
python3 SMB_AUTOMATION/run_all.py list
python3 SMB_AUTOMATION/run_all.py run all --env productqa --wave 2
python3 SMB_AUTOMATION/create_smb_welcome_user.py --phases preflight                 # pilot readiness only
python3 SMB_AUTOMATION/create_smb_welcome_user.py --ensure-pilot-config              # set the pilot configs (backed up first)
```

`--manifest` re-checks the same user: it waits for the email again and re-runs every check, without creating a new user.

| flow | user | email |
|---|---|---|
| welcome / welcome_gas_nsm | Electric AMI 960 / Gas NSM 961 | USER_WELCOME |
| monthly_summary, budget_billing | Electric AMI 960 | MONTHLY_SUMMARY |
| demand_journey | Electric AMI 963 | MONTHLY_SUMMARY with demand charges |
| tou_monthly_summary | Electric AMI 962 | MONTHLY_SUMMARY with TOU usage |
| monthly_summary_gas_ami_no_itemization_gas_chart / monthly_summary_gas_nsm_no_itemization_gas_chart | Gas AMI / Gas NSM 961 | MONTHLY_SUMMARY (gas): no itemization (never rendered for SMB gas), gas bar chart, billing-charges block off |
| bill_projection / tou_bill_projection / bill_projection_gas_ami | Electric AMI 960 / 962 / Gas AMI 961 | BILL_PROJECTION |
| budget_alert / budget_alert_gas_ami | Electric AMI 960 / Gas AMI 961 | BUDGET_ALERT (75%) |
| tou_onboarding | Electric AMI 960, then USERENROLL to 962 | TOU_ONBOARDING |
| sbc_dual_fuel | Electric AMI 960 + Gas NSM 961 | NEIGHBOURHOOD_COMPARISON (combined) |
| file_upload_* | Electric AMI / Gas NSM / both | none (data checks only) |

## New pilot: step by step

Run every command from `masterpilot-04/`. `<env>` is the name of the target file in `targets/` (`productqa`, `uat`, `dev`,
`productqa-88012`, or your new one). Tokens come from `.env` or the environment (see Setup), never from the command line.

1. **Target file.** Copy `targets/template.json.example` to `targets/<env>.json` and fill in: `pilot`, `base_url`,
   `dashboard_url`, `launchpad_bucket` (the pilot's `s3_pull` bucket), `marker_bucket`, `bill_cycle_code`, `utility_name`,
   `timezone`, the plan names, `aggregation_priority_queue`, `sbc_donor_ntypes`, and the `token.env` variable name. Put the
   token under that name in `.env`.
2. **Plan (read-only).** Shows what is missing; writes nothing:
   ```bash
   python3 SMB_AUTOMATION/setup_pilot.py --env <env>
   ```
3. **Apply.** Copies the rate plans by name from the reference (productqa 88004), creates the bill-cycle code, copies the
   hybrid models and the reco model files (reco files come from uat, productqa serves recommendations from CMS), then writes
   the pilot configs, string prerequisites and mail settings (each config type is backed up first and read back):
   ```bash
   python3 SMB_AUTOMATION/setup_pilot.py --env <env> --apply
   ```
4. **Ingestion bucket (only if the pilot's ingestion must read the target's buckets).** This reroutes the pilot's files:
   ```bash
   python3 SMB_AUTOMATION/setup_pilot.py --env <env> --apply --set-ingestion-bucket
   ```
5. **By a person or DevOps** (the script prints this list and does not do it): the SparkPost key
   `email_moderation.smtp_pwd` at pilot level, servers up, the ingester reading a new bucket, the DCX feature rows, and
   neighbourhood clusters for the SBC comparison.
6. **Preflight (read-only).** Fix everything it lists before running any email:
   ```bash
   python3 SMB_AUTOMATION/create_smb_welcome_user.py --env <env> --phases preflight
   ```
7. **Run one email, then all.** Keep the wave small (the queues are shared):
   ```bash
   python3 SMB_AUTOMATION/run_all.py run welcome --env <env>
   python3 SMB_AUTOMATION/run_all.py run all --env <env> --wave 2
   ```
   Results are in `results/<flow>/run_<env>_<stamp>/`; the console log of each script is in `results/_runner/`.

Notes for a new pilot:
- Gas Monthly Summary: the two gas flows above set `email_monthly_summary_section_configs.show_billing_charges_in_billing_section=false`
  at user scope. Without it the gas MS email is never stored (`EMAIL_TEMPLATE_NOT_RENDERED`, Velocity `Variable
  $monthlyUsageEnergyUsageChargesTextKey has not been set` at `monthly_summary_section_v2.vm` line 151): pingpong's
  `MonthlySummarySectionContentProvider.getMonthSummaryTemplateData` sets the charges text keys for electric and water only.
  The Hawk gas MS scenarios need the same user-level config until pingpong sets the keys for gas.
- `--allow-pilot-drift` runs a flow although a REQUIRED pilot config differs (it warns and writes nothing).
- Do not use `--apply` / `--ensure-pilot-config` on a pilot that already holds values you need: they change pilot-level
  configs (backups are in `<run>/pilot_config_backups/`).
- Drop reasons for a missing email: the backend writes one JSON record per notification attempt to
  `s3://common-metrics-productqa/notification-events/YYYY/MM/DD/HH/` on productqa (UTC, about 10 minutes behind; the
  bucket name of other envs is not checked).

## Checks

| id | check | source of truth |
|---|---|---|
| C01 | SMB segment, enabled, every meter token, plan | `meta/users`, `homes/1`, `gws/*/meters` |
| C02 | interval data ingested up to the last days | `streams/.../labelTimestamps` |
| C03 | every invoiced cycle = the generated invoice TOTAL | `billingdata/.../utilitydata` |
| E01-E03 | email stored, subject, fully rendered (no `${}`, null, NaN) | `2.1/utility_notifications` |
| E04 | every section in the user's resolved section list is rendered | `entities/user/.../email_template_sections_config` |
| M01-M05 | bill amount = billing_cost of the last cycle, period, demand cost, BB amount, TOU wording | `consumption/ctypes/billing_cost`, generated invoice |
| B01-B03 | spent, projected, days left | `2.1/users/.../billprojections` |
| A01-A03 | 75% wording, budget = home threshold, spent | `homes/1`, `billprojections` |
| W01-W02, T01-T02, N01-N02 | greeting/portal, TOU plan and peak window, comparison amounts | `homes/1`, email text |

Output: `results/<flow>/run_<env>_<stamp>/` holds `SUMMARY.txt`, `manifest.json`, `input_files/`, `logs/run.log`,
`reports/validation_report.{md,json}`, `email/<TYPE>.html` and `api_evidence/<check>.json`.

## Notes

- Each flow writes user-scope configs and strings only. Exception: `set_bill_cycle` creates the pilot's `BCC_MP04_<day>`
  bill-cycle rows, and only when that code is missing.
- Any pilot, any env: everything target-specific comes from `targets/<env>.json` (pilot, URLs, bucket, plan names,
  bill-cycle code, `utility_name`, `sbc_donor_ntypes`, optional `hybrid_model_bucket`). The flows use `${dashboardUrl}`,
  `USERPROGRAM_<utility_name>_` and an SBC neighbourhood found in that env (Hawk's 88001/381481938 when it exists,
  else the largest non-solar cluster of the pilot or a donor ntype). For a new pilot: copy `targets/template.json.example`,
  copy the rate plans (`rate_migration/`), the bill cycle (`copy_bill_cycle.py`), the hybrid models
  (`copy_hybrid_models.py`), then run `--phases preflight --ensure-pilot-config` once and `--phases preflight` after.
- User scope: each flow writes exactly the user configs and strings of its Hawk masterpilot-04 scenario (checked
  key by key: 0 differences across the 18 scenarios; Hawk's Java writes no configs of its own).
- Email strings, read at run time: the strings step reads every email text of the reference pilot (`string_source` in
  `targets/<env>.json`, default productqa 88004) with one call, compares it with the new user's resolved texts (one call)
  and seeds only the missing ones on that user; `data/email_strings.json` is used only if the reference cannot be read.
- Email strings (MASTERPILOT-02 style): every email's footer dereferences the utility address text, and the Monthly
  Summary and Bill Projection emails dereference their "new service"
  texts without a null guard, so a missing one NPEs the whole email (`NewServiceSectionTemplateUpdator:50`). productqa has
  them at pilot scope, UAT 88004 does not. The engine adds a step after the user's configs (`EVENT_STRINGS` in
  `smb_common.py`) that reads each text for the new user and, only where it resolves to nothing, seeds it on that user
  (read back; the run stops if it still resolves to nothing). Shared pilot/global strings are never written; where the
  texts already exist the step does nothing.
- PREFLIGHT pilot readiness (MASTERPILOT-02 style), first in every script and read-only by default (`sk/pilot_setup.py`).
  `--ensure-pilot-config` sets the configs that differ (each type backed up to `<run>/pilot_config_backups/`, read back);
  `--phases preflight` checks only; `run_all.py ... --ensure-pilot-config` writes once before the first wave.

  | Item | Level | Why |
  |---|---|---|
  | rate plans the flows use (SMB_GS_STANDARD, SMB_GAS_STANDARD, SMB_GS_TOU, SMB_GS_DEMAND) | REQUIRED, not fixed by --ensure | users are enrolled on them by name |
  | bill-cycle code `bill_cycle_code` on the pilot | REQUIRED, not fixed (`copy_bill_cycle.py`) | USERENROLL references it |
  | `hybrid-models-<env>/masterpilot/US_<pilot>_{AMI,NSM}_electric.xlsx` | REQUIRED, not fixed (`copy_hybrid_models.py`) | itemization; S3 404 aborts aggregation |
  | launchpad bucket `launchpad_bucket` exists | REQUIRED, not fixed (DevOps / the pilot's `s3_pull`) | every launchpad file goes there |
| `insights_and_reco_moderation.peak_hour_groups` not `""` | REQUIRED (removed: made absent, never given a value) | `""` aborts every aggregation |
  | `hybrid_moderation.is_pure_statistical_hybrid_enabled=false` | REQUIRED | SMB itemization categories |
  | `hybrid_moderation.csv_filename_format=Home.country-User.pilotId-UserType.meterType` | REQUIRED | names the model files `US_<pilot>_<meterType>_<fuel>` |
  | `meta_data.gas_unit=com.bidgely.cloud.unitType.therms` | REQUIRED | gas emails; empty gives itemization HTTP 400 |
  | `neighbourhood_comparison.data_point_threshold=0` | REQUIRED | SBC comparison with few users |
  | `meta_data.supported_communication_events` contains the flow's email event (`USER_WELCOME`, `TOU_ONBOARDING`, …) | REQUIRED (the event is appended, existing events kept) | the notifications processor skips an unlisted event with only a DEBUG log: no processor line, no notification row, the email never arrives (productqa 88012, 2026-11-09) |
  | `email_moderation` `email_sending_service=sparkpost`, `smtp_username=SMTP_Injection:X-MSYS-SUBACCOUNT=55`, `is_bcc_forced=false` | REQUIRED | otherwise GLOBAL smtp: every email is built but FAILED |
  | `email_moderation.smtp_pwd` (SparkPost key) at pilot level | REQUIRED, **checked only, never written** (a person sets it) | the key SparkPost sends with |
  | `email_template.feedback_url` = the hash-style URL (`…feedback?delivery-mode=${delivery-mode}&feedback-type=…`) | REQUIRED | the GLOBAL default needs `${uuid}`, which is not set when `append_user_hash=true`: the feedback section throws and the whole email (Welcome, …) is dropped |
  | `email_template.unsubscribe_url_opt_out_mails` = `<dashboard_url>/dashboard/settingsPreferences?tab-type=${tab-type}&event-type=${event-type}` | REQUIRED | the GLOBAL default needs `${uuid}`: every email's footer throws "Variable $uuid has not been set at unsubscribe" |
  | Missing-only keys (`MISSING_ONLY_CONFIGS`): `program_part_launchpad` (10), `budget_billing_configs` (4), `bb_cumulative_balance_section_configs` (1) and its palette (8), productqa's values | REQUIRED for every flow; only absent keys are added, existing values never changed | the program file mapping creates `BB_PROGRAM_1`; the BB settings render the budget-billing section (an undefined colour aborts the whole Monthly Summary) |
  | 152 keys in `data/pilot_empty_placeholders.json` not `""` at pilot | REQUIRED (removed) | `""` broke USERENROLL user creation |
  | Welcome image URLs `""`, `email_footer_section_configs.description_for_ms=""`, `budget_alert_mails_recommendations_count=4` | WARN | email matches the Hawk-approved one |
  | `rate_comparison.rate_plans_comparison_map` SMB entries (numbers looked up by plan name) | WARN | TOU / rate comparison content |
  | `rate_comparison.enable_appliance_breakdown`, `minimum_appliance_itemization_confidence` not `""` | WARN (removed) | a newer build aborts aggregation on them |

- Never written at pilot scope: `event_subscriptions.*`, `email_template_sections_config` (DCX) and
  `insights_and_reco_moderation.peak_hour_groups` (an empty one is removed with --ensure-pilot-config, never set).
- The flows were written once from the masterpilot-04 emails generated in Hawk (same user shape, seed data, configs and
  triggers). Nothing is read from Hawk at run time.

## What an environment needs before the emails generate

Found while bringing up uat (2026-11-04). Check these first when no email arrives; each failed the aggregation or the
notification pass before any email was built.

| Prerequisite | Symptom when missing | How to check |
|---|---|---|
| 88004 hybrid models in `hybrid-models-<env>/masterpilot/US_88004_{AMI,AMR,NSM}_{electric,gas}*` | `HybridRuleEngine.getModelLastUpdatedTimestamp` S3 404 during `ApplianceAggregateHandler.populateMonthAggregate` | list the bucket |
| Recommendation model: 88004 recommendations in CMS (`GET 2.0/cms/recommendation-models/pilot/88004` -> `valid=true`), or `cms_config.reco_cms_enabled=false` plus `masterpilot/US_88004_<type>_reco*` files in the hybrid bucket | `createModelUsingCMSFlow:447` "Failed to fetch recommendation model data from CMS", then an NPE at `RecommendationsRunner.run:197`; caught, recommendations dropped | the endpoint above |
| `insights_and_reco_moderation.peak_hour_groups` ABSENT at pilot (an empty value is not the same as absent) | `ApplianceConfigImpl.getPeakHourGroup` "Illegal peak hour groups string", aggregation aborts before the notification pass | `GET entities/pilot/88004/configs/insights_and_reco_moderation` |
| No empty numeric `rate_comparison` placeholders (`minimum_appliance_itemization_confidence`) on a newer build | `getRateComparisonConfigBean` `NumberFormatException: empty String`, aggregation aborts | `GET entities/pilot/88004/configs/rate_comparison` |
| Delivery modes and section lists for the email (DCX, or equivalent pilot values): `event_subscriptions.<EVENT>.delivery_modes` `["Email"]`, `email_template_sections_config.<EVENT>.OPT_OUT` | `EMAIL_ALREADY_SENT` drop with no delivery mode; empty sections cannot build the email | `GET entities/user/<uuid>/configs/...` for a test user |
| `email_moderation.user_welcome=go` and `meta_data.supported_communication_events` including the event | Welcome blocked or event not raised | pilot config |

An empty placeholder written by a config sync can break any of the above again; re-check after a sync.
