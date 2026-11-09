"""PREFLIGHT pilot readiness for every SMB run on any pilot / env (MASTERPILOT-02 mds_common.preflight style).

Read-only unless --ensure-pilot-config is given. Everything is resolved from the target (targets/<env>.json): pilot id,
plan names, bill-cycle code, hybrid model bucket. Nothing names 88004 or productqa.

REQUIRED (a miss stops the run with "pilot not ready: ..."; --ensure-pilot-config fixes the configs, never the rest):
  - rate plans the flows enrol users on exist on the pilot (by name)          -> copy with rate_migration/
  - the target's bill-cycle code exists                                         -> copy_bill_cycle.py
  - hybrid models masterpilot/US_<pilot>_{AMI,NSM}_electric.xlsx in the bucket  -> copy_hybrid_models.py
  - the launchpad bucket (targets launchpad_bucket) exists                       -> DevOps / the pilot's s3_pull
  - REQUIRED_CONFIGS below (the code reads them at pilot scope only), incl. email_template.feedback_url
  - MISSING_ONLY_CONFIGS (program file mapping, budget-billing configs, BB section + palette); missing keys only
  - no "" placeholder of data/pilot_empty_placeholders.json at PILOT (an empty pilot value broke
    PresentationService.createUserAndHome: NumberFormatException For input string "")
  - insights_and_reco_moderation.peak_hour_groups not "" at PILOT (removed so it is absent; never given a value)
MATCH_HAWK (a miss is a WARN: the run passes its checks, but the email differs from the Hawk-approved one):
  - MATCH_HAWK_CONFIGS below and the SMB rate_comparison map entries
  - NEWER_BUILD_EMPTY: "" values harmless on productqa's build, fatal to aggregation on a newer one
Never touched: event_subscriptions.*, email_template_sections_config (DCX). User-scope configs and strings are set by
each flow on its own user (smb_flows.py, identical to the Hawk masterpilot-04 scenarios).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from .api import Api, SetupError
from .target import T

ROOT = Path(__file__).resolve().parent.parent
PLACEHOLDERS = ROOT / "data" / "pilot_empty_placeholders.json"

REQUIRED_CONFIGS = {
    # SMB disaggregation: the SMB model path instead of the residential hybrid V2 (same on productqa/uat/dev 88004;
    # a pilot copied from a residential template, e.g. 88012, has enable_smb=false / hybrid V2 on)
    "disagg_preference": {"enable_smb": "true", "enable_hybrid_v2": "false", "gas_nsm_disagg_enabled": "false"},
    # SMB electric itemization in MS / BB / demand emails (the kit's pilot value true gave residential categories)
    "hybrid_moderation": {"is_pure_statistical_hybrid_enabled": "false",
                          "fall_back_to_cost_with_fix_charges": "false",
                          "supported_charge_types": "CONSUMPTION_BASED:.*,CONSUMPTION_BASED:null",
                          # model file name masterpilot/US_<pilot>_<meterType>_<fuel>*: what copy_hybrid_models.py writes
                          "csv_filename_format": "Home.country-User.pilotId-UserType.meterType"},
    # gas emails and gas itemization (empty -> itemization HTTP 400 "kWh and kW")
    "meta_data": {"gas_unit": "com.bidgely.cloud.unitType.therms"},
    # SBC / neighbourhood comparison generates with the few users of a test pilot
    "neighbourhood_comparison": {"data_point_threshold": "0"},
    # Mail delivery: the pilot's own SparkPost sending, as productqa/dev/UAT 88001/88002 hold it. Without these the pilot
    # falls back to GLOBAL smtp (subaccount 7, forced BCC) and every email is built but FAILED (UAT 88004 2026-11-05).
    # The SparkPost key (smtp_pwd) is a credential: checked below, never written by this kit.
    "email_moderation": {"email_sending_service": "sparkpost", "smtp_username": "SMTP_Injection:X-MSYS-SUBACCOUNT=55",
                         "is_bcc_forced": "false"},
    # With append_user_hash=true the feedback section's URL template only gets ${delivery-mode} ${feedback-type}
    # ${event-type} ${locale}; the GLOBAL default (https://bidgely.com/email/feedback?uuid=${uuid}&type=${type}) needs
    # ${uuid}, so EmailURLProcessingManager.getFeedbackPageUrls throws "Variable $uuid has not been set" and every email
    # with a FEEDBACK section (Welcome, ...) is dropped (uat 2026-11-05). A bidgely.com URL, the same on every env.
    "email_template": {"feedback_url": "https://www.bidgely.com/feedback?delivery-mode=${delivery-mode}"
                                       "&feedback-type=${feedback-type}&event-type=${event-type}&locale=${locale}",
                       # Same failure in the footer (EmailFooterSectionContentProvider.getOptOutFooterTemplate:190,
                       # "Variable $uuid has not been set at unsubscribe"): the GLOBAL default
                       # https://bidgely.com/email/unsubscribe?uuid=${uuid} needs ${uuid}. Each env points at its own
                       # dashboard (productqa/dev hold exactly this value), so it is built from T.dashboard_url.
                       "unsubscribe_url_opt_out_mails": T.dashboard_url.rstrip("/") + "/dashboard/settingsPreferences"
                                                        "?tab-type=${tab-type}&event-type=${event-type}"},
}
MATCH_HAWK_CONFIGS = {
    "email_user_welcome_section_configs": {"appliance_usage_image_url": "", "similar_homes_comparison_image_url": "",
                                           "saving_tips_image_url": ""},
    "email_footer_section_configs": {"description_for_ms": ""},
    "recommendation_preference": {"budget_alert_mails_recommendations_count": "4"},
}
# Pilot keys any email may depend on, as productqa 88004 has them. MASTERPILOT-02 mds_bb.ensure_pilot_bb style:
# only keys the pilot lacks are added (backed up first), an existing value is never changed. REQUIRED for every flow:
# a gap stops the run until --ensure-pilot-config fills it.
#   program_part_launchpad   launchpad column mapping of the USERPROGRAM file. Without it the file is never read (no
#                            Error marker either): no BB_PROGRAM_1, isBBUser=false (dev 88004 2026-11-05)
#   budget_billing_configs   BB program per fuel and the averaging the budget-billing amount uses
#   bb_cumulative_balance_section_configs(_palette)  the BB_CUMULATIVE_BALANCE section and its 8 colours (an undefined
#                            ${colorNN} aborts the whole Monthly Summary render)
MISSING_ONLY_CONFIGS = {
    "program_part_launchpad": {
        "account_id": '{"fieldPosition":1,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"USER_ACCOUNT_ID"}',
        "customer_id": '{"fieldPosition":0,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"CUSTOMER_ID"}',
        "is_eligible": '{"fieldPosition":6,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"IS_ELIGIBLE"}',
        "is_participating": '{"fieldPosition":7,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"IS_PARTICIPATING"}',
        "premise_id": '{"fieldPosition":2,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"PREMISE_ID"}',
        "program_attr": '{"fieldPosition":9,"utilityFieldMandatory":false,"regex":null,"programParticipationField":"PROGRAM_ATTRIBUTES"}',
        "program_id": '{"fieldPosition":3,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"PROGRAM_ID"}',
        "task_id": '{"fieldPosition":8,"utilityFieldMandatory":false,"regex":null,"programParticipationField":"TASK_ID"}',
        "user_part_end_date": '{"fieldPosition":5,"utilityFieldMandatory":false,"regex":null,"programParticipationField":"USER_PARTICIPATION_END_DATE"}',
        "user_part_start_date": '{"fieldPosition":4,"utilityFieldMandatory":true,"regex":null,"programParticipationField":"USER_PARTICIPATION_START_DATE"}',
    },
    "budget_billing_configs": {
        "average_bc_duration": "30",
        "bb_invoice_after_program_start": "false",
        "bb_program_ids_map": "{\"ELECTRIC\": \"BB_PROGRAM_1\", \"WATER\": \"BB_PROGRAM_3\"}",
        "no_of_bcs_for_average": "2",
    },
    "bb_cumulative_balance_section_configs": {
        "color_palette_id": "bb_cumulative_balance_section_configs_palette",
    },
    "bb_cumulative_balance_section_configs_palette": {
        "color1": "#333333",
        "color4": "#FFFFFF",
        "color43": "#F6F6F6",
        "color86": "#FE8E25",
        "color87": "#088090",
        "color88": "#1F73E3",
        "color89": "#2E384D",
        "color90": "#616F89",
    },
}

# Email-section configs copied from the reference pilot (T.string_source, the same reference the strings step reads),
# read at run time: every key the reference holds at PILOT that the target resolves to nothing is added (missing-only,
# existing values never changed); the reference's dashboard host is rewritten to the target's. The 4 Nov 2026 sync
# skipped URL values, which left UAT without footer social links, CTA URLs, BB arrows ... (2026-11-05 gap report).
REF_CONFIG_SKIP = ("email_moderation", "email_template_sections_config", "event_subscriptions", "sms_", "her_",
                   "seasonal_alert", "launchpad", "ingestion", "s3_", "data_")


# non-email types emails also read: Bill Projection numbers (projection / budget alert) and notification thresholds
REF_EXTRA_TYPES = ("bill_projection", "notification_event_config")
SECRET_KEY = re.compile(r"token|pwd|password|secret|api_?key|credential|username", re.I)


def is_email_config_type(ct: str) -> bool:
    low = ct.lower()
    return ((low.startswith("email_") or "section_config" in low or low.endswith("_palette")) and
            not any(low.startswith(x) for x in REF_CONFIG_SKIP)) or low in REF_EXTRA_TYPES


def reference_config_gaps(api: Api) -> dict[str, dict[str, str]]:
    """{type: {key: value}} the target pilot lacks, from the reference pilot's email-section configs."""
    src = T.string_source or {}
    env, pilot = src.get("env"), int(src.get("pilot") or 0)
    if not env or (env == os.environ.get("MP04_ENV", "productqa") and pilot == T.pilot):
        return {}
    from copy_hybrid_models import Env
    ref_env = Env(env)
    ref_host = json.loads((ROOT / "targets" / f"{env}.json").read_text())["dashboard_url"].rstrip("/")

    def all_cfg(call, pid) -> dict[str, dict]:
        body = call("GET", f"entities/pilot/{pid}/configs")
        res = body.get("payload", body) if isinstance(body, dict) else {}
        out = {}
        for ct, raw in (res or {}).items():
            if not is_email_config_type(ct):
                continue
            try:
                out[ct] = {kv["key"]: kv for kv in (json.loads(raw) if isinstance(raw, str) else raw).get("kvs") or []}
            except (TypeError, ValueError, AttributeError):
                continue
        return out
    ref = all_cfg(ref_env.call, pilot)
    tgt = all_cfg(lambda m, p: api.call(m, p)["body"], T.pilot)
    gaps: dict[str, dict[str, str]] = {}
    for ct, kvs in ref.items():
        for k, kv in kvs.items():
            val = str(kv.get("val") or "")
            if kv.get("configSource") != "PILOT" or not val.strip():
                continue
            if SECRET_KEY.search(k):                # credentials (access tokens, passwords, keys) are never copied
                continue
            if str((tgt.get(ct, {}).get(k) or {}).get("val") or "").strip():
                continue
            val = val.replace(ref_host, T.dashboard_url.rstrip("/"))
            if env in val:                          # another env-specific value (bucket, host): never copied
                continue
            gaps.setdefault(ct, {})[k] = val
    return gaps


# "" at PILOT that only newer builds parse as numbers (uat 2026-11-04: getRateComparisonConfigBean NumberFormatException
# "empty String", aggregation aborts); productqa's older build ignores them: WARN, removed with --ensure-pilot-config
NEWER_BUILD_EMPTY = {"rate_comparison": ["enable_appliance_breakdown", "minimum_appliance_itemization_confidence"]}
# rate_comparison.rate_plans_comparison_map: each SMB electric plan compared with the other two (numbers looked up by name)
RATE_COMPARISON_PLANS = ("SMB_GS_STANDARD", "SMB_GS_TOU", "SMB_GS_DEMAND")


def flow_email_event(flow: str | None) -> str | None:
    """The email event (notification type) the flow generates, e.g. TOU_ONBOARDING; None when the flow has none."""
    if not flow:
        return None
    from smb_flows import FLOWS
    return ((FLOWS.get(flow) or {}).get("email") or {}).get("type")


def flow_plan_names() -> set[str]:
    from smb_flows import FLOWS
    names = {T.plan("electric")["name"], T.plan("gas")["name"]}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("ratePlanId", "ratePlanIdGas", "ratePlanIdElectric", "rateId") and isinstance(v, str):
                    names.add(v)
                walk(v)
        elif isinstance(o, (list, tuple)):
            for v in o:
                walk(v)
    walk([f["steps"] for f in FLOWS.values()])
    return names


def config_gaps(api: Api, wanted: dict, writes: dict, why: list[str]) -> None:
    for ctype, kv in wanted.items():
        cur = api.read_config("pilot", T.pilot, ctype)
        for k, v in kv.items():
            c = cur.get(k) or {}
            if c.get("configSource") != "PILOT" or c.get("val") != v:
                writes.setdefault(ctype, {})[k] = v
                why.append(f"{ctype}.{k}={c.get('val')!r:.40} [{c.get('configSource')}] (needs {v!r} at PILOT)")


def plan(api: Api, flow: str | None = None) -> dict:
    """What is wrong now: {'fatal': [...], 'writes': {...}, 'removals': {...}, 'required': [...], 'optional': [...]}."""
    p = {"fatal": [], "writes": {}, "removals": {}, "required": [], "optional": []}
    phg = api.read_config("pilot", T.pilot, "insights_and_reco_moderation").get("peak_hour_groups") or {}
    if phg.get("configSource") == "PILOT" and phg.get("val") == "":     # removed (absent), never given a value
        p["removals"].setdefault("insights_and_reco_moderation", []).append("peak_hour_groups")
        p["required"].append('insights_and_reco_moderation.peak_hour_groups is "" at PILOT: every aggregation aborts '
                             "(ApplianceConfigImpl.getPeakHourGroup)")
    key = api.read_config("pilot", T.pilot, "email_moderation").get("smtp_pwd") or {}
    if key.get("configSource") != "PILOT" or not str(key.get("val") or "").strip():
        p["fatal"].append(f"email_moderation.smtp_pwd (the SparkPost key) is not set at pilot level [{key.get('configSource')}]: "
                          "a person must set it (same as a working pilot of this env); the kit never writes credentials")
    # Recommendations: from the pilot's CMS model, or with reco_cms_enabled=false from reco model files in the hybrid
    # bucket. A pilot with neither gets "Failed to fetch recommendation model data from CMS" and drops every reco.
    cms = api.payload("GET", f"2.0/cms/recommendation-models/pilot/{T.pilot}", allow=(400, 404, 500)) or {}
    cms_ok = isinstance(cms, dict) and bool(cms.get("valid"))
    if not cms_ok:
        reco_on = str((api.read_config("pilot", T.pilot, "cms_config").get("reco_cms_enabled") or {}).get("val")).lower()
        if reco_on != "false":
            p["writes"].setdefault("cms_config", {})["reco_cms_enabled"] = "false"
            p["required"].append("no valid CMS recommendation model and reco_cms_enabled is not false: recommendations "
                                 "would fail (set to false; the reco model files then come from the hybrid bucket)")
        p["reco_files_needed"] = True
    plans = api.payload("GET", f"v3.0/rates/configuration/utilityId/{T.pilot}", allow=(400, 404)) or []
    by_name = {x.get("planName"): x.get("planNumber") for x in plans if isinstance(x, dict)}
    missing = sorted(flow_plan_names() - set(by_name))
    if missing:
        p["fatal"].append(f"rate plans {missing} missing on pilot {T.pilot}: copy them with rate_migration/")
    cycles = api.payload("GET", f"2.1/utilityBillingCycles/utility/{T.pilot}", allow=(400, 404)) or {}
    rows = cycles.get("data", []) if isinstance(cycles, dict) else cycles
    if not any(str(r.get("billCycleCode")) == T.bill_cycle_code for r in rows or []):
        p["fatal"].append(f"bill-cycle code {T.bill_cycle_code} (targets bill_cycle_code) not set up on pilot {T.pilot}: "
                          "copy it with copy_bill_cycle.py")
    bucket = T.hybrid_model_bucket or f"hybrid-models-{os.environ.get('MP04_ENV', 'productqa')}"
    try:
        import boto3
        s3 = boto3.client("s3", region_name=T.aws_region)
        di = api.read_config("pilot", T.pilot, "data_ingestion")
        for rk in ("s3BucketForErrorReports", "s3BucketForSuccessReports"):
            rv = str((di.get(rk) or {}).get("val") or "")
            if T.marker_bucket and rv != T.marker_bucket:
                p["optional"].append(f"data_ingestion.{rk} is {rv!r}, not this env's external bucket {T.marker_bucket!r} "
                                     "(ingestion reports go elsewhere); --set-ingestion-bucket fixes it")
        pilot_bucket = str((di.get("s3BucketName") or {}).get("val") or "")
        if pilot_bucket != T.launchpad_bucket:
            p["fatal"].append(f"pilot data_ingestion.s3BucketName is {pilot_bucket!r} but targets launchpad_bucket is "
                              f"{T.launchpad_bucket!r}: files uploaded there are not read as pilot {T.pilot}. The pilot's "
                              "s3_pull/data_ingestion bucket (ingestion config, set by DevOps) must name the same bucket")
        try:
            s3.head_bucket(Bucket=T.launchpad_bucket)
        except Exception as e:  # noqa: BLE001
            p["fatal"].append(f"launchpad bucket s3://{T.launchpad_bucket} (targets launchpad_bucket = the pilot's "
                              f"s3_pull bucket) not reachable: {e}")
        if p.get("reco_files_needed"):
            key = f"masterpilot/US_{T.pilot}_AMI_reco.xlsx"
            if s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1).get("KeyCount", 0) == 0:
                p["fatal"].append(f"no CMS recommendation model and no reco model files (s3://{bucket}/{key}): copy them "
                                  "with copy_hybrid_models.py --reco-source-env uat")
        for mt in ("AMI", "NSM"):
            key = f"masterpilot/US_{T.pilot}_{mt}_electric.xlsx"
            if s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1).get("KeyCount", 0) == 0:
                p["fatal"].append(f"hybrid model s3://{bucket}/{key} missing: copy with copy_hybrid_models.py")
    except Exception as e:  # noqa: BLE001
        p["fatal"].append(f"cannot list s3://{bucket} for the hybrid models: {e}")

    config_gaps(api, REQUIRED_CONFIGS, p["writes"], p["required"])
    # AbstractEventProcessor.processValidUser:147 skips an event the pilot does not list in
    # meta_data.supported_communication_events, logging only at DEBUG: no processor line, no notification row, the
    # email just never comes (productqa 88012 lacked TOU_ONBOARDING, 2026-11-09). Append the flow's event to the
    # pilot's own list; existing events are kept as they are.
    ev = flow_email_event(flow)
    if ev:
        cur = api.read_config("pilot", T.pilot, "meta_data").get("supported_communication_events") or {}
        have = [x.strip() for x in str(cur.get("val") or "").split(",") if x.strip()]
        if ev not in have:
            p["writes"].setdefault("meta_data", {})["supported_communication_events"] = ",".join([*have, ev])
            p["required"].append(f"meta_data.supported_communication_events lacks {ev} [{cur.get('configSource')}]: the "
                                 f"notifications processor skips {ev} silently (adds it to the pilot's list)")
    for ctype, keys in json.loads(PLACEHOLDERS.read_text()).items():
        cur = api.read_config("pilot", T.pilot, ctype)
        for k in keys:
            c = cur.get(k)
            if c and c.get("configSource") == "PILOT" and c.get("val") == "":
                p["removals"].setdefault(ctype, []).append(k)
    if p["removals"]:
        p["required"].append(f"{sum(len(v) for v in p['removals'].values())} empty placeholder key(s) at PILOT in "
                             f"{sorted(p['removals'])}")

    for ctype, keys in NEWER_BUILD_EMPTY.items():
        cur = api.read_config("pilot", T.pilot, ctype)
        empty = [k for k in keys if (cur.get(k) or {}).get("configSource") == "PILOT" and (cur.get(k) or {}).get("val") == ""]
        if empty:
            p["removals"].setdefault(ctype, []).extend(empty)
            p["optional"].append(f"{ctype} {empty} are \"\" at PILOT: a newer build aborts aggregation on them")
    for ctype, kv in MISSING_ONLY_CONFIGS.items():
        cur = api.read_config("pilot", T.pilot, ctype)
        missing = {k: v for k, v in kv.items() if not (cur.get(k) or {}).get("val")}
        if missing:
            p["writes"].setdefault(ctype, {}).update(missing)
            p["required"].append(f"{ctype}: {len(missing)} of {len(kv)} key(s) missing at PILOT ({sorted(missing)[:4]})")
    try:
        ref_gaps = reference_config_gaps(api)
    except (SystemExit, Exception) as e:  # noqa: BLE001 -- reference unreadable: report, do not block
        ref_gaps = {}
        p["optional"].append(f"reference email configs not readable ({e})")
    if ref_gaps:
        for ct, kv in ref_gaps.items():
            p["writes"].setdefault(ct, {}).update(kv)
        n = sum(len(v) for v in ref_gaps.values())
        p["optional"].append(f"{n} email-section config key(s) the reference pilot has are missing here, in "
                             f"{len(ref_gaps)} type(s) ({', '.join(sorted(ref_gaps)[:6])}{' ...' if len(ref_gaps) > 6 else ''})")
    config_gaps(api, MATCH_HAWK_CONFIGS, p["writes"], p["optional"])
    nums = [by_name.get(n) for n in RATE_COMPARISON_PLANS]
    if all(nums):
        want = {str(n): [m for m in nums if m != n] for n in nums}
        c = api.read_config("pilot", T.pilot, "rate_comparison").get("rate_plans_comparison_map") or {}
        try:
            m = json.loads(c.get("val") or "{}")
        except ValueError:
            m = None
        if m is None:
            p["optional"].append(f"rate_comparison.rate_plans_comparison_map is not JSON: {c.get('val')!r:.80}")
        elif any(m.get(k) != v for k, v in want.items()):
            p["writes"]["rate_comparison"] = {"rate_plans_comparison_map": json.dumps({**m, **want})}
            p["optional"].append(f"rate_comparison.rate_plans_comparison_map lacks {want}")
    return p


def backup(api: Api, run_dir: Path, ctype: str, log) -> Path:
    raw = api.call("GET", f"entities/pilot/{T.pilot}/configs/{ctype}", allow=(400, 404, 500))["body"]
    p = run_dir / "pilot_config_backups" / f"BACKUP_pilot{T.pilot}_{ctype}_{datetime.now():%Y%m%d_%H%M%S}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(raw, indent=2))
    log(f"backed up pilot {ctype} -> {p}")
    return p


# (config type, key, target attribute): where the pilot's launchpad files are read from, and the env's external bucket
# that ingestion error/success reports and pull files use (a pilot copied from another env's template points at that
# env's bucket, e.g. 88012 on productqa had bidgely-masterpilot-dev-external)
INGESTION_BUCKET_KEYS = [("s3_pull", "s3DestinationBucket", "launchpad_bucket"),
                         ("data_ingestion", "s3BucketName", "launchpad_bucket"),
                         ("data_ingestion", "s3BucketForErrorReports", "marker_bucket"),
                         ("data_ingestion", "s3BucketForSuccessReports", "marker_bucket"),
                         ("s3_pull", "s3PullBucketName", "marker_bucket")]


def set_ingestion_bucket(api: Api, run_dir: Path, log=print) -> None:
    """Point the pilot's ingestion at targets launchpad_bucket (s3_pull.s3DestinationBucket + data_ingestion.s3BucketName).
    Reroutes every file of the pilot, so only with the explicit --set-ingestion-bucket flag; backed up, read back."""
    for ctype, key, attr in INGESTION_BUCKET_KEYS:
        want = getattr(T, attr)
        if not want:
            continue
        cur = (api.read_config("pilot", T.pilot, ctype).get(key) or {}).get("val")
        if cur == want:
            log(f"pilot {T.pilot} {ctype}.{key} already {want}")
            continue
        backup(api, run_dir, ctype, log)
        api.call("POST", f"entities/{T.pilot}/configs", {"configType": ctype, "configKVs": [
            {"configKey": key, "configVal": want, "configDataType": "TEXT", "configRegex": "(?s).*",
             "configDocumentation": f"{ctype}.{key} set by MP04 SMB_AUTOMATION --set-ingestion-bucket"}]})
        got = (api.read_config("pilot", T.pilot, ctype).get(key) or {}).get("val")
        if got != want:
            raise SetupError(f"{ctype}.{key} did not read back: {got!r}")
        log(f"pilot {T.pilot} {ctype}.{key}: {cur!r} -> {want!r}")


def preflight(api: Api, run_dir: Path, log=print, ensure: bool = False, flow: str | None = None,
              set_bucket: bool = False) -> None:
    log(f"PREFLIGHT pilot {T.pilot} readiness on {T.name} (read-only unless --ensure-pilot-config is given)")
    if set_bucket:
        set_ingestion_bucket(api, run_dir, log)
    p = plan(api, flow)
    if p["fatal"]:
        raise SetupError("pilot not ready: " + "; ".join(p["fatal"]))
    if not ensure:
        for w in p["optional"]:
            log(f"  WARN: {w}")
        if p["required"]:
            raise SetupError("pilot not ready: " + "; ".join(p["required"]) +
                             ". Re-run with --ensure-pilot-config to set them (pilot-level, backed up first)")
        log(f"pilot {T.pilot}: ready" + (f" ({len(p['optional'])} WARN)" if p["optional"] else ""))
        return
    if not p["writes"] and not p["removals"]:
        log(f"pilot {T.pilot}: ready")
        return
    for ctype in sorted({*p["writes"], *p["removals"]}):
        backup(api, run_dir, ctype, log)
    for ctype, kv in p["writes"].items():
        api.call("POST", f"entities/{T.pilot}/configs", {"configType": ctype, "configKVs": [
            {"configKey": k, "configVal": v, "configDataType": "TEXT", "configRegex": "(?s).*",
             "configDocumentation": f"{ctype}.{k} set by MP04 SMB_AUTOMATION --ensure-pilot-config"} for k, v in kv.items()]})
        log(f"pilot {T.pilot} {ctype}: wrote {sorted(kv)}")
    for ctype, keys in p["removals"].items():
        api.call("DELETE", f"v2.0/configs/{ctype}/pilot/{T.pilot}", keys)
        log(f"pilot {T.pilot} {ctype}: removed {len(keys)} empty key(s)")
    left = plan(api, flow)
    if left["required"]:                       # warnings (optional) are reported, not treated as a failed write
        raise SetupError("pilot config did not read back: " + "; ".join(left["required"]))
    log(f"pilot {T.pilot}: ready (set and read back)")
