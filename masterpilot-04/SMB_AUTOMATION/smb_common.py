"""smb_common: the engine behind every create_smb_*_user.py (MASTERPILOT-02 mds_common style) for pilot 88004 SMB.

A flow (smb_flows.FLOWS) is an ordered list of steps: create the launchpad user, set its user-scope configs and strings,
shift and upload the seed RAW files (data/), build and upload the invoice priced on the user's own billing cycles,
trigger the platform and wait for the email. Then validate() runs numbered checks against the platform's own APIs:
user and meters, ingested RAW and invoices, the stored email, its subject, every configured section, and the figures
the email shows (bill cost, projection, budget, demand, budget billing) against billing_cost / billprojections / home.
"""
from __future__ import annotations

import html as html_lib
import json
import math
import os
import random
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from sk import pilot_setup  # noqa: E402
from sk.api import Api, SetupError  # noqa: E402
from sk.target import T  # noqa: E402

DATA = ROOT                                     # flow paths are data/<file>

TZ = ZoneInfo(T.timezone)
HOME = 1
VAR = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


class StepFailed(Exception):
    pass


# Which email a reference text belongs to, by its id (first match wins). A flow seeds only the texts of its own email
# plus the common ones (no group matched: footer, legal, feedback, greeting, CTA ...). `()` = no SMB flow uses it.
STRING_GROUPS = [
    ((".email.ev.", "evmonthlytracker", "managed_charging"), ()),                       # EV
    (("solar",), ()),                                                                     # solar
    ((".email.tou.", "teaser.tou"), ("tou",)),                                            # flows with "tou" in the key
    (("monthlysummary", ".ms.", "monthly.usage", "zero.cost", "total.usage", "energy.charge"), ("MONTHLY_SUMMARY",)),
    (("billprojection", ".bp.", "energy.savings.tips"), ("BILL_PROJECTION",)),
    (("budgetalert", "budget.alert"), ("BUDGET_ALERT",)),
    ((".shc.", "sbc", "neighbourhood", "3barnc", "dualfuel", ".df."), ("NEIGHBOURHOOD_COMPARISON",)),
    (("welcome", "singlebutton"), ("USER_WELCOME",)),
]


def string_applies(rid: str, event: str, flow: str) -> bool:
    low = rid.lower()
    for needles, owners in STRING_GROUPS:
        if any(n in low for n in needles):
            return event in owners or any(o.islower() and o in flow for o in owners)
    return True                                                                          # common to every email


# Texts the email's "new service" block dereferences without a null guard (NewServiceSectionTemplateUpdator): a missing
# one NPEs the whole email. productqa holds them at pilot scope, an environment may not (UAT 88004, 2026-11-05). As in
# MASTERPILOT-02 (mds_bb BB_STRINGS) the flow seeds them on its own test user, only when they resolve to nothing there;
# text = what productqa resolves. Hawk writes none of them, so with them present nothing changes.
EVENT_STRINGS = {
    # every email: the footer dereferences the utility_address string (EmailFooterSectionContentProvider
    # .getOptOutFooterTemplate:150); UAT 88004 lacked it (2026-11-05), productqa/dev hold it at pilot scope
    # Fallback only: the email texts are read at run time from T.string_source (reference_strings); this snapshot of
    # productqa 88004 (2026-11-05) is used when that source cannot be read
    "*": {
        **json.loads((ROOT / "data" / "email_strings.json").read_text()),
        "com.bidgely.cloud.address.string": "128 Saratoga Ave - River Valley - ST - 11245 - 1-234-567-89009",
    },
    "MONTHLY_SUMMARY": {
        "com.bidgely.cloud.email.monthlySummary.newService.title.text":
            "Welcome to your new Home Energy Report from Energy Co.!",
        "com.bidgely.cloud.email.monthlySummary.newService.body.text":
            "You'll now receive personalized emails that will help you better understand your energy usage, as well as "
            "provide tips to save on your monthly bill.",
        "com.bidgely.cloud.email.ms.newService.title.text": "Welcome to your new Home Energy Report from Energy Co.!",
        # usage-chart title when this cycle equals last year's (EmailMonthlyChartSectionContentProvider
        # .getChartHeaderTemplate:790, no null guard). Missing on productqa, dev and UAT alike (2026-11-05), so the run-time
        # copy cannot supply it: same wording as productqa's peak.same counterpart
        "com.bidgely.cloud.email.monthly.usage.title.total.same": "See your usage history below",
    },
    "BILL_PROJECTION": {
        "com.bidgely.cloud.email.billProjection.newService.title.text": "Your Bill Summary",
        "com.bidgely.cloud.email.billProjection.newService.body.text": "You will receive new emails each month",
    },
}


# TOU_RATE_STRUCTURE reads the plan's own title/description with stringMap.get(<key>).getText() and no null guard
# (TouRateStructureSectionContentProvider:104,110): a missing one NPEs the whole email before any notification is
# stored (dev 88004, 2026-11-09). The key names come from the target pilot's own plan rows (rateStructureTitle /
# rateStructureDescription), so the plan NUMBER may differ per environment/pilot. The keys carry no "email", so the
# generic reference copy skips them; the text is read from the reference pilot, else this snapshot (productqa 88004).
TOU_PLAN_NAME = "SMB_GS_TOU"
PLAN_STRINGS = {
    TOU_PLAN_NAME: {
        "title": "How does your Small Business Time-of-Use rate work?",
        "description": "On the Small Business Time-of-Use rate, electricity costs more during On-Peak hours (12 PM to 6 PM, "
                       "Monday to Friday) and less during Off-Peak hours, all other times and all weekend. Running "
                       "equipment outside 12 PM - 6 PM on weekdays lowers your bill.",
    },
}


LOG_FILE: Path | None = None


def log(msg: str) -> None:
    line = f"[{datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")


def as_list(v):
    return v if isinstance(v, list) else [v]


def rand_digits(n: int) -> str:
    return "".join(random.choice("0123456789") for _ in range(n))


def subst(text: str, vars_: dict) -> str:
    """commons-lang3 StrSubstitutor: ${name} from vars, ${name:-default}, unknown names left untouched."""
    def rep(m):
        k, dflt = m.group(1), m.group(2)
        if k in vars_ and vars_[k] is not None:
            return str(vars_[k])
        return dflt if dflt is not None else m.group(0)
    return VAR.sub(rep, text)


def tmpfile(prefix: str, suffix: str) -> Path:
    """java.io.File.createTempFile(prefix, suffix): prefix + random long + suffix in the temp dir."""
    return Path(tempfile.gettempdir()) / f"{prefix}{random.randrange(10**18, 10**19)}{suffix}"


class Engine:
    def __init__(self, key: str, flow: dict, run_dir: Path, api: Api | None = None):
        self.key, self.flow = key, flow
        self.steps = list(flow["steps"])
        ev = (flow.get("email") or {}).get("type")
        if ev:                                      # seed missing email texts once the user's configs are set
            first_data = next((i for i, (n, _) in enumerate(self.steps) if n == "define"), len(self.steps))
            self.steps.insert(first_data, ("ensure_event_strings", {}))
        self.api = api or Api()
        self.run_dir = run_dir
        self.set_bucket = False
        # target-specific values the flows reference as ${dashboardUrl} / userProgramFilePrefix (never hard-coded)
        self.vars: dict = {"home": HOME, "dashboardUrl": T.dashboard_url.rstrip("/"),
                           "userProgramFilePrefix": f"USERPROGRAM_{T.utility_name}_"}
        self.results: list[tuple[str, str, str]] = []
        self.uploads: list[str] = []
        self._s3 = None

    # ------------------------------------------------------------------ plumbing
    def lookup(self, x):
        """BaseConfig.getSubstitutedValue: a variable name if present, else the literal."""
        return self.vars.get(x, x) if isinstance(x, str) else x

    def data_path(self, x: str) -> list[Path]:
        v = self.vars.get(x)
        if v is not None:
            return [Path(p) for p in str(v).split(",")]
        out = []
        for part in x.split("|"):
            p = DATA / part.lstrip("/")
            out += sorted(p.iterdir()) if p.is_dir() else [p]
        return out

    def get(self, path: str, allow=(404, 400, 500)):
        r = self.api.call("GET", path, allow=allow)
        return r["body"]

    def post(self, path: str, body, allow=()):
        return self.api.call("POST", path, body, allow=allow)

    @property
    def uuid(self) -> str:
        return self.vars["userUUID"]

    def s3(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3", region_name=T.aws_region)
        return self._s3

    def label_ts(self, fuel: str = "ELECTRIC") -> dict:
        b = self.get(f"streams/users/{self.uuid}/homes/{HOME}/labelTimestamps/{fuel.upper()}/Default") or {}
        return b.get("payload", b) if isinstance(b, dict) else {}

    def billing_cycles(self, fuel: str = "ELECTRIC") -> list[tuple[int, int]]:
        lt = self.label_ts(fuel)
        t0, t1 = lt.get("first"), lt.get("last")
        if not t0:
            return []
        b = self.get(f"billingdata/users/{self.uuid}/homes/{HOME}/billingcycles?t0={t0}&t1={t1}&measurementType={fuel.upper()}")
        return sorted((int(e["key"]), int(e["value"])) for e in (b if isinstance(b, list) else []) if e.get("key"))

    # ------------------------------------------------------------------ run
    def run(self, resume: dict | None = None, ensure_pilot: bool = False, preflight_only: bool = False) -> int:
        """PREFLIGHT pilot readiness, then every step (or, with resume = an earlier manifest, only wait_email on that
        same user), then validate()."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log(f"===== {self.key}: {self.flow['title']} on {T.name} | run dir {self.run_dir}")
        steps = self.steps
        if not resume:
            try:
                pilot_setup.preflight(self.api, self.run_dir, log, ensure_pilot, self.key, self.set_bucket)
            except SetupError as e:
                log(f"PREFLIGHT FAILED: {e}")
                self.check("P00", "pilot readiness", "FAIL", e)
                self.write_report("FAIL")
                return 1
            if preflight_only:
                return 0
        if resume:
            self.vars.update(resume.get("vars", {}))
            self.uploads = resume.get("uploads", [])
            self.vars["messageSentAfter"] = int(self.vars.get("messageSentAfter") or time.time() - 86400)
            steps = [(n, c) for n, c in self.steps if n == "wait_email"]
            log(f"resume on user {self.uuid}: wait_email + validate only")
        broke = None
        try:
            for name, cfg in steps:
                log(f"--- {name}")
                getattr(self, f"s_{name}")(cfg)
                self.save_manifest()
        except (StepFailed, SetupError) as e:
            broke = str(e)
            log(f"STEP FAILED: {e}")
        except KeyboardInterrupt:
            self.save_manifest()
            raise
        if self.vars.get("userUUID"):
            try:
                self.validate()
            except (StepFailed, SetupError) as e:
                self.check("X01", "validation", "FAIL", f"validation stopped: {e}")
        if broke:
            self.check("X00", "flow step", "FAIL", broke)
        status = "FAIL" if any(r[2] == "FAIL" for r in self.results) else "PASS"
        self.save_manifest()
        self.write_report(status)
        return 0 if status == "PASS" else 1

    def check(self, cid: str, name: str, status: str, detail: str, evidence=None) -> None:
        self.results.append((cid, name, status, str(detail)))
        if evidence is not None:
            (self.run_dir / "api_evidence").mkdir(parents=True, exist_ok=True)
            (self.run_dir / "api_evidence" / f"{cid}.json").write_text(json.dumps(evidence, indent=1, default=str))
        log(f"  [{status}] {cid} {name}: {str(detail)[:200]}")

    def save_manifest(self) -> None:
        keep = {k: v for k, v in self.vars.items() if isinstance(v, (str, int, float, bool)) and not k.startswith("_")}
        (self.run_dir / "manifest.json").write_text(json.dumps(
            {"flow": self.key, "env": T.name, "vars": keep, "uploads": self.uploads}, indent=1, default=str))

    def write_report(self, status: str) -> None:
        rep = self.run_dir / "reports"
        rep.mkdir(parents=True, exist_ok=True)
        rows = "\n".join(f"| {c} | {n} | {st} | {d[:220]} |" for c, n, st, d in self.results)
        user = (f"- env `{T.name}` pilot {T.pilot}\n- user `{self.vars.get('userUUID')}` email `{self.vars.get('email')}`\n"
                f"- customer {self.vars.get('customerId')} contract {self.vars.get('contractId')} premise "
                f"{self.vars.get('premiseId')} plan {self.vars.get('ratePlanId')}\n"
                f"- email: {self.vars.get('_event', '-')} '{self.vars.get('_title', '-')}' id {self.vars.get('notificationId', '-')}\n"
                f"- uploads: {', '.join(self.uploads)}\n")
        md = f"# {self.flow['title']}: {status}\n\n{user}\n| Check | Name | Status | Details |\n|---|---|---|---|\n{rows}\n"
        (rep / "validation_report.md").write_text(md)
        (rep / "validation_report.json").write_text(json.dumps(
            {"flow": self.key, "status": status, "env": T.name, "userUUID": self.vars.get("userUUID"),
             "email": self.vars.get("email"), "checks": [dict(zip(("id", "name", "status", "details"), r)) for r in self.results]},
            indent=1))
        (self.run_dir / "SUMMARY.txt").write_text(md)
        log(f"===== {self.key}: {status} uuid={self.vars.get('userUUID')} report={rep / 'validation_report.md'}")

    # ------------------------------------------------------------------ user creation
    def s_create_user(self, cfg: dict) -> None:
        """New SMB launchpad user: one USERENROLL row (one meter) or two rows (Electric AMI + Gas NSM, dualFuel)."""
        dual = bool(cfg.get("dualFuel"))
        overrides = dict(cfg.get("overrides") or {})
        contract = rand_digits(9)
        base = {
            "customerId": rand_digits(9), "contractId": contract, "premiseId": rand_digits(9), "accountType": "SMB",
            "email": f"bidgelyqa+AUT_MP04_{contract}@bidgely.com", "firstName": "SMB", "lastName": "OFFICE",
            "addressLine1": "221B Baker Street", "addressLine2": "SUITE 100", "addressLine3": "", "addressLine4": "",
            "city": "CHICAGO", "state": "IL", "zip": "60601", "regionCode": "",
            "mailingAddress1": "558 COMMERCE ST", "mailingAddress2": "SUITE 100", "mailingAddress3": "",
            "mailingAddress4": "", "mailingCity": "CHICAGO", "mailingState": "IL", "mailingZip": "60601",
            "phoneNumber": rand_digits(10), "phoneType": "Home", "language": "EN", "dataConsentStatus": "OBTAINED",
            "completeUnsubscribe": "false", "serviceAgreementStartDate": "2021-06-15", "serviceAgreementEndDate": "",
            "ratePlanId": T.plan("electric")["name"], "ratePlanEffectiveDate": "2021-06-15", "billingCycleCode": T.bill_cycle_code,
            "billingCycleEffectiveDate": "2021-06-15", "solar": "FALSE", "label": "",
        }
        if os.environ.get("MP04_EMAIL"):                    # optional recipient override (must be bidgely.com)
            base["email"] = os.environ["MP04_EMAIL"]
        fields = ("customerId|contractId|premiseId|accountType|email|firstName|lastName|addressLine1|addressLine2|"
                  "addressLine3|addressLine4|city|state|zip|regionCode|mailingAddress1|mailingAddress2|mailingAddress3|"
                  "mailingAddress4|mailingCity|mailingState|mailingZip|phoneNumber|phoneType|language|dataConsentStatus|"
                  "completeUnsubscribe|dataStreamId|serviceType|serviceAgreementStartDate|serviceAgreementEndDate|ratePlanId|"
                  "ratePlanEffectiveDate|billingCycleCode|billingCycleEffectiveDate|solar|dataStreamType|label").split("|")
        if dual:
            rows = []
            for suffix, stype, dtype in (("Electric", "ELECTRIC", "AMI"), ("Gas", "GAS", "NSM")):
                v = dict(base, dataStreamId=rand_digits(9), serviceType=stype, dataStreamType=dtype)
                for k, val in overrides.items():
                    if k.endswith(suffix):
                        v[k[: -len(suffix)]] = val
                    elif not (k.endswith("Electric") or k.endswith("Gas")):
                        v[k] = val
                rows.append(v)
            el, gas = rows
            self.vars.update(dataStreamIdElectric=el["dataStreamId"], dataStreamIdGas=gas["dataStreamId"],
                             dataStreamId=el["dataStreamId"], measurementType="ELECTRIC", measurementTypeTwo="GAS")
            token_row, content = el, "|".join(el[f] for f in fields) + "\n" + "|".join(gas[f] for f in fields)
        else:
            v = dict(base, dataStreamId=rand_digits(9), serviceType="ELECTRIC", dataStreamType="AMI")
            v.update({k: (self.vars.get(x, x)) for k, x in overrides.items() if k != "bucket"})
            self.vars.update(dataStreamId=v["dataStreamId"], measurementType=v["serviceType"])
            token_row, content = v, "|".join(v[f] for f in fields)
        f = tmpfile(f"USERENROLL_D_{datetime.now():%Y%m%d%H%M}_01", ".txt")
        f.write_text(content)
        self.upload(f)
        keep = {k: self.vars[k] for k in ("dataStreamId", "dataStreamIdElectric", "dataStreamIdGas") if k in self.vars}
        self.vars.update(token_row)                         # every USERENROLL field is a run variable
        self.vars.update(keep)
        tok = (f"{T.pilot}:{base['customerId']}_{base['contractId']}_{base['premiseId']}_{token_row['dataStreamId']}_"
               f"{token_row['dataStreamType']}")
        time.sleep(30)
        deadline = time.time() + 900
        while True:
            for t in (tok, tok + ":inactive"):
                r = self.api.call("GET", f"meta/tokens/{t}", allow=(400, 404, 500))
                body = r["body"]
                txt = body if isinstance(body, str) else \
                    ((body or {}).get("raw") or (body or {}).get("payload")) if isinstance(body, dict) else None
                if r["status"] == 200 and txt and str(txt).count("/") >= 2:
                    self.vars["userUUID"] = str(txt).split("/")[2]
                    break
            if "userUUID" in self.vars:
                break
            if time.time() >= deadline:
                raise StepFailed(f"meta token {tok} not resolved within 15 min (USERENROLL {f.name})")
            time.sleep(20)
        self.vars.update(pilotId=str(T.pilot), externalUserId=base["contractId"], token=f"{T.pilot}:{base['customerId']}__",
                         timeZoneCode=T.timezone, bheHerUser=self.uuid)
        log(f"USER CREATED uuid={self.uuid} email={self.vars['email']} meters="
            + ("ELECTRIC AMI + GAS NSM" if dual else f"{token_row['serviceType']} {token_row['dataStreamType']}")
            + f" plan={token_row['ratePlanId']}")

    def upload(self, f: Path) -> None:
        """Upload to the launchpad bucket: key = file name, metadata utility_file_name, 10 s pause after each file."""
        if not f.exists() or f.stat().st_size == 0:
            log(f"upload skipped (missing/empty): {f}")
            return
        self.s3().put_object(Bucket=T.launchpad_bucket, Key=f.name, Body=f.read_bytes(),
                             Metadata={"utility_file_name": f.name})
        self.uploads.append(f.name)
        (self.run_dir / "input_files").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "input_files" / f.name).write_bytes(f.read_bytes())
        log(f"uploaded s3://{T.launchpad_bucket}/{f.name} ({f.stat().st_size} bytes)")
        time.sleep(10)

    # ------------------------------------------------------------------ user / config steps
    def s_update_user(self, cfg: dict) -> None:
        self.post(f"meta/users/{self.uuid}", cfg)

    def s_update_home(self, cfg: dict) -> None:
        self.post(f"meta/users/{self.uuid}/homes/{HOME}", {k: subst(str(v), self.vars) for k, v in cfg.items()})

    def s_user_configs(self, cfg) -> None:
        for c in as_list(cfg):
            ent = self.uuid + c.get("entitySuffix", "")
            # A key with no stored metadata in that env needs documentation + data type + regex, else HTTP 500
            # "Required fields are missing for configKey ..." (ConfigManager.validateConfigs; seen on uat 2026-11-04)
            kv = {"configKey": c["configKey"], "configVal": subst(str(c["configVal"]), self.vars),
                  "configDataType": c.get("configDataType", "TEXT"), "configRegex": c.get("configRegex", "(?s).*"),
                  "configDocumentation": c.get("configDocumentation", "set by MP04 SMB_AUTOMATION")}
            self.post(f"entities/{ent}/configs", {"configType": c["configType"], "configKVs": [kv]})
        log(f"{len(as_list(cfg))} user config key(s) written")

    def s_user_strings(self, cfg) -> None:
        for c in as_list(cfg):
            ent = self.uuid + c.get("entitySuffix", "")
            path = f"2.1/stringResources/{ent}/resource/{c['resourceId']}"
            body = [{"locale": "en_US", "text": subst(c["text"], self.vars)}]
            r = self.api.call("PUT", path, body, allow=(400, 404, 500))
            ok = r["status"] == 200 and ((r["body"] or {}).get("payload") or {}).get("status")
            if not ok and c.get("createIfMissing"):
                self.api.call("POST", path, body, allow=(400, 404, 500))
        log(f"{len(as_list(cfg))} user string resource(s) written")

    def s_sbc_cluster(self, cfg: dict) -> None:
        """Neighbourhood for the SBC comparison: the cluster the Hawk test used when it exists in this environment, else
        the largest non-solar (preferably DUAL_FUEL) cluster of the target pilot or a donor in T.sbc_donor_ntypes."""
        def clusters(nt) -> list[dict]:
            p = self.get(f"nhs/{nt}/ids") or []
            p = p.get("payload") if isinstance(p, dict) else p
            return p if isinstance(p, list) else []
        pref_nt, pref_id = str(cfg.get("preferNTypeId", "")), str(cfg.get("preferNhoodId", ""))
        if pref_nt and any(str(c.get("nid")) == pref_id for c in clusters(pref_nt)):
            nt, nid = pref_nt, pref_id
        else:
            best = None
            for cand in (T.pilot, *T.sbc_donor_ntypes):
                for c in clusters(cand):
                    am = c.get("attributeMap") or {}
                    if str(am.get("isSolarUser", "False")).lower() == "true":
                        continue
                    rank = (am.get("fuelTypeTag") == "DUAL_FUEL", int(c.get("userCount") or 0))
                    if best is None or rank > best[0]:
                        best = (rank, str(cand), str(c.get("nid")))
            if not best:
                raise StepFailed(f"no non-solar SBC cluster in ntypes {[T.pilot, *T.sbc_donor_ntypes]} on {T.name}: "
                                 "the neighbourhood pipeline (SHC clusters) has not run there")
            _, nt, nid = best
        self.vars.update(sbcNTypeId=nt, sbcNhoodId=nid)
        log(f"SBC neighbourhood ntype {nt} nid {nid}" + (" (Hawk's)" if (nt, nid) == (pref_nt, pref_id) else ""))

    def reference_strings(self) -> dict[str, str]:
        """Email texts of the reference pilot (T.string_source, any env with a targets file), read at run time with one
        call: every resolved id that is an email text. {} when the target is the reference itself; the
        EVENT_STRINGS["*"] snapshot when the reference cannot be read."""
        src = T.string_source or {}
        env, pilot = src.get("env"), int(src.get("pilot") or 0)
        if not env or (env == os.environ.get("MP04_ENV", "productqa") and pilot == T.pilot):
            return {}
        if "_ref_strings" not in self.vars:
            try:
                from copy_hybrid_models import Env
                body = Env(env).call("GET", f"2.1/stringResources/pilot/{pilot}?locale=en_US")
                res = body.get("payload") if isinstance(body, dict) else None
                self.vars["_ref_strings"] = {k: v for k, v in (res or {}).items() if isinstance(v, str) and v.strip()
                                             and ("email" in k.lower() or ".rate.structure." in k
                                                  or k == "com.bidgely.cloud.address.string")}
                log(f"{len(self.vars['_ref_strings'])} email strings read from {env} pilot {pilot}")
            except (SystemExit, Exception) as e:  # noqa: BLE001 -- no token / host down: keep the snapshot
                log(f"WARN reference strings from {env} pilot {pilot} not readable ({e}): using the saved snapshot")
                self.vars["_ref_strings"] = dict(EVENT_STRINGS["*"])
        return self.vars["_ref_strings"]

    def tou_plan_strings(self) -> dict[str, str]:
        """{string id: text} of the target pilot's TOU plan title/description, whatever its plan number is here.
        The ids are read from the pilot's own plan rows (the same fields TouRateStructureDataProvider reads); the text
        from the reference pilot when it has the id, else PLAN_STRINGS. {} when the plan or its ids cannot be read."""
        if "_tou_plan_strings" not in self.vars:
            found: dict[str, str] = {}
            try:
                body = self.get(f"v3.0/rates/configuration/utilityId/{T.pilot}") or {}
                plans = body.get("payload") if isinstance(body, dict) else None
                for plan in plans or []:
                    if plan.get("planName") != TOU_PLAN_NAME:
                        continue
                    for comp in plan.get("components") or []:
                        for rate in comp.get("rates") or []:
                            for field, kind in (("rateStructureTitle", "title"), ("rateStructureDescription", "description")):
                                rid = rate.get(field)
                                if rid:
                                    found[rid] = (self.reference_strings().get(rid)
                                                  or PLAN_STRINGS[TOU_PLAN_NAME][kind])
                if not found:
                    log(f"WARN no rate-structure string ids on plan {TOU_PLAN_NAME} of pilot {T.pilot} on {T.name}: "
                        "nothing to seed (the TOU rate structure section will not render)")
                else:
                    log(f"plan {TOU_PLAN_NAME}: {len(found)} rate-structure string id(s): {', '.join(sorted(found))}")
            except (SystemExit, Exception) as e:  # noqa: BLE001 -- keep the run going; the email check will tell
                log(f"WARN cannot read {TOU_PLAN_NAME} plan strings on {T.name} ({e})")
            self.vars["_tou_plan_strings"] = found
        return self.vars["_tou_plan_strings"]

    def missing_event_strings(self, event: str) -> dict[str, str]:
        """Reference email texts (plus this event's EVENT_STRINGS) that resolve to nothing for the user
        (user -> pilot -> global), from one call for the user's whole resolved set."""
        want = {**{k: v for k, v in self.reference_strings().items()
                   if ".rate.structure." not in k and string_applies(k, event, self.key)},
                **EVENT_STRINGS.get(event, {})}
        if event == "TOU_ONBOARDING":
            want.update(self.tou_plan_strings())
        r = self.get(f"2.1/stringResources/user/{self.uuid}?locale=en_US") or {}
        have = r.get("payload") if isinstance(r, dict) else None
        have = have if isinstance(have, dict) else {}
        return {rid: text for rid, text in want.items() if not str(have.get(rid) or "").strip()}

    def s_ensure_event_strings(self, cfg) -> None:
        """MASTERPILOT-02 style: seed the event's new-service texts at user scope only where missing, then read back."""
        ev = (self.flow.get("email") or {}).get("type")
        missing = self.missing_event_strings(ev)
        if not missing:
            log(f"{ev} email strings already resolve for the user: nothing to seed")
            return
        log(f"WARN {len(missing)} {ev} string(s) missing for the user: seeding at user scope "
            f"({', '.join(sorted(missing)[:5])}{' ...' if len(missing) > 5 else ''})")
        for rid, text in missing.items():           # missing at user scope: create directly (PUT only updates)
            body = [{"locale": "en_US", "text": text}]
            r = self.api.call("POST", f"2.1/stringResources/{self.uuid}/resource/{rid}", body, allow=(400, 404, 409, 500))
            if r["status"] != 200:
                self.api.call("PUT", f"2.1/stringResources/{self.uuid}/resource/{rid}", body, allow=(400, 404, 500))
        still = self.missing_event_strings(ev)
        if still:
            raise StepFailed(f"email strings still missing after seeding: {sorted(still)}")
        log(f"{len(missing)} email string(s) seeded and read back")

    def s_ensure_event_section(self, cfg: dict) -> None:
        """MASTERPILOT-02 style (mds_bb.bb_sections): put a section into the user's Monthly Summary OPT_IN and OPT_OUT
        lists, only where it is missing, before the first existing section listed in `before`, then read back.
        The environment's DCX list may omit it (dev 88004 2026-11-05 lacked BB_CUMULATIVE_BALANCE, productqa has it)."""
        section, before = cfg["section"], cfg.get("before", [])
        ev = cfg.get("event", "MONTHLY_SUMMARY")
        r = self.get(f"entities/user/{self.uuid}/configs/email_template_sections_config?context={ev}&measurement-type=ELECTRIC",
                     allow=(400, 404, 500)) or {}
        r = r.get("payload", r) if isinstance(r, dict) else {}
        kv = {x["key"]: x for x in json.loads((r or {}).get("email_template_sections_config") or "{}").get("kvs", [])}
        kvs = []
        for mode in ("OPT_IN", "OPT_OUT"):
            key = f"{ev}.{mode}"
            if key not in kv:
                continue
            lst = json.loads(kv[key]["val"])
            if any(x.get("emailTemplateSection") == section for x in lst):
                continue
            pos = next((i for i, x in enumerate(lst) if x.get("emailTemplateSection") in before), len(lst))
            lst.insert(pos, {"emailTemplateSection": section, "mandatory": False})
            kvs.append({"configKey": key, "configVal": json.dumps(lst, separators=(",", ":")), "configDataType": "TEXT",
                        "configRegex": "(?s).*", "configDocumentation": f"{section} added by MP04 SMB_AUTOMATION ({ev})"})
        if not kvs:
            log(f"{ev} section list already has {section}: nothing to add")
            return
        self.post(f"entities/{self.uuid}/configs", {"configType": "email_template_sections_config", "configKVs": kvs})
        r = self.get(f"entities/user/{self.uuid}/configs/email_template_sections_config?context={ev}&measurement-type=ELECTRIC",
                     allow=(400, 404, 500)) or {}
        r = r.get("payload", r) if isinstance(r, dict) else {}
        kv = {x["key"]: x for x in json.loads((r or {}).get("email_template_sections_config") or "{}").get("kvs", [])}
        still = [k for k in (x["configKey"] for x in kvs)
                 if not any(x.get("emailTemplateSection") == section for x in json.loads(kv.get(k, {}).get("val") or "[]"))]
        if still:
            raise StepFailed(f"{section} not in {still} after the write")
        log(f"WARN {section} added to {ev} section list at user scope ({len(kvs)} list(s)), read back")

    def s_set_bill_cycle(self, cfg: dict) -> None:
        """Home bill-cycle code BCC_MP04_<day of today+offset> (created on the pilot only if missing) + enrollmentTime 2 years back."""
        h = self.get(f"meta/users/{self.uuid}/homes/{HOME}") or {}
        h = h.get("payload", h)
        tz = ZoneInfo(h.get("timezone") or T.timezone)
        now = datetime.now(tz)
        day = (now + timedelta(days=int(cfg.get("offsetDurationFromToday", 0)))).day
        code = f"BCC_MP04_{day}"
        b = self.get(f"2.1/utilityBillingCycles/utility/{T.pilot}/identifier/{code}") or {}
        rows = b.get("payload") or [] if isinstance(b, dict) else []
        exists = bool(rows) and int(rows[-1].get("validTo") or 0) >= time.time() + 30 * 86400
        if not exists:
            log(f"bill cycle code {code} missing on pilot {T.pilot}: creating its 12 rows")
            for fm, tm, fd, td in self.cycles_starting_at(day):
                self.post("2.1/utilityBillingCycles", {
                    "billCycleCode": code, "utilityID": str(T.pilot), "validFrom": "1000000000", "validTo": "2000000000",
                    "fromMonth": str(fm), "toMonth": str(tm), "fromDay": str(fd), "toDay": str(td),
                    "utilityName": h.get("utilityName") or ""}, allow=(400,))
        self.post(f"meta/users/{self.uuid}/homes/{HOME}", {"billCycleCode": code})
        enroll = now.replace(year=now.year - 2, day=day, microsecond=0) if day <= 28 else \
            now.replace(year=now.year - 2, day=min(day, 28), microsecond=0)
        enroll = enroll.replace(hour=0, minute=0)
        self.post(f"meta/users/{self.uuid}/homes/{HOME}", {"enrollmentTime": str(int(enroll.timestamp()))})
        self.vars["billCycleCode"] = code
        log(f"home billCycleCode={code}" + (" (existing)" if exists else " (created)"))

    @staticmethod
    def cycles_starting_at(d: int) -> list[tuple]:
        if d == 1:
            ml = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
            return [(m, m, 1, ml[m]) for m in range(12)]
        if d == 29:
            return [(m, (m + 1) % 12, 29, 28) for m in range(12)]
        if d == 30:
            return [(0, 1, 30, 27), (1, 2, 28, 29)] + [(m, (m + 1) % 12, 30, 29) for m in range(2, 12)]
        if d == 31:
            return [(0, 1, 31, 27), (1, 2, 28, 30), (2, 3, 31, 29), (3, 4, 30, 30), (4, 5, 31, 29), (5, 6, 30, 30),
                    (6, 7, 31, 30), (7, 8, 31, 29), (8, 9, 30, 30), (9, 10, 31, 29), (10, 11, 30, 30), (11, 0, 31, 30)]
        return [(m, (m + 1) % 12, d, d - 1) for m in range(12)]

    # ------------------------------------------------------------------ file steps
    def s_shift_raw(self, cfg) -> None:
        """Shift a seed RAW file (dataTillStartOfDayAtOffset): one epoch delta for every
        row so the LAST row's label lands on local midnight at today + processValue."""
        for c in as_list(cfg):
            tz = TZ if c.get("usePilotTimezone") else datetime.now().astimezone().tzinfo
            files = self.data_path(c["dataFile"])
            target = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) \
                + timedelta(days=int(c.get("processValue", 0)))
            if c.get("processHandle") == "dataTillCurrentTime":
                target = datetime.now(tz)
            parse = lambda s: datetime.strptime(s.strip(), "%Y%m%d%H%M%S").replace(tzinfo=tz).timestamp()
            ends = []
            for f in files:
                last = [l for l in f.read_text().splitlines() if l.strip()][-1]
                ends.append(parse(last.split("|")[4]))
            offset = int(target.timestamp()) - int(max(ends))
            outs = []
            for f in files:
                lines = []
                for l in f.read_text().splitlines():
                    e = l.split("|")
                    if len(e) < 6 or not e[4].strip():
                        continue
                    ts = datetime.fromtimestamp(parse(e[4]) + offset, tz).strftime("%Y%m%d%H%M%S")
                    lines.append("|".join(e[:4] + [ts] + e[5:8]))
                out = tmpfile(c["dataTag"], ".txt")
                out.write_text(os.linesep.join(lines))
                outs.append(str(out))
            self.vars[c["dataTag"]] = ",".join(outs)
            log(f"shifted {', '.join(p.name for p in files)} by {offset} s -> last label "
                f"{datetime.fromtimestamp(max(ends) + offset, tz):%Y-%m-%d %H:%M} {tz}")

    def s_fill_ids(self, cfg) -> None:
        for c in as_list(cfg):
            m = {k: str(self.vars.get(v, v)) for k, v in (c.get("substitutionMap") or {}).items()}
            outs = []
            for f in self.data_path(c["sourceFile"]):
                out = tmpfile("StringSubstitution_ProcessedFile_", ".tmp")
                out.write_text(os.linesep.join(subst(l, m) for l in f.read_text().splitlines()))
                outs.append(str(out))
            self.vars[c["name"]] = ",".join(outs)

    def s_name_file(self, cfg) -> None:
        """Launchpad file name: prefix + yyyyMMddHHmmssSSS + "-000" + suffix (machine local time)."""
        for c in as_list(cfg):
            src = c.get("source")
            paths = str(self.vars[c["value"]]).split(",") if src == "ref" else [c["value"]]
            pre, suf = str(self.lookup(c.get("filePrefix", ""))), str(self.lookup(c.get("fileSuffix") or ".tmp"))
            outs = []
            for p in paths:
                while True:
                    n = datetime.now()
                    out = Path(tempfile.gettempdir()) / f"{pre}{n:%Y%m%d%H%M%S}{n.microsecond // 1000:03d}-000{suf}"
                    if not out.exists():
                        break
                    time.sleep(0.002)
                out.write_bytes(Path(p).read_bytes())
                outs.append(str(out))
            self.vars[c["name"]] = ",".join(outs)
            if "raw" in pre.lower():
                d = re.search(r"\d+", pre)
                if d:
                    self.vars["duration"] = int(d.group())

    def s_upload(self, cfg) -> None:
        for c in as_list(cfg):
            for p in str(self.vars.get(c["file"], c["file"])).split(","):
                self.upload(Path(p))

    def s_sleep(self, cfg: dict) -> None:
        s = int(cfg.get("sleepForSeconds", 0))
        log(f"sleep {s}s: {cfg.get('message', '')}")
        time.sleep(s)

    def s_define(self, cfg) -> None:
        for c in as_list(cfg):
            fn, name = c.get("generateFunction"), c["name"]
            ad = c.get("additionalData") or {}
            if fn is None:
                self.vars[name] = c.get("value")
            elif fn == "currenttimestamp":
                self.vars[name] = int(time.time())
            elif fn in ("formattedBillStart@today", "formattedBillEnd@today"):
                bcs = self.billing_cycles(ad.get("measurementType", "ELECTRIC"))
                i = len(bcs) - 1 - int(ad.get("offsetFromCurrentBC", 0))
                if i < 0:
                    raise StepFailed(f"{name}: no billing cycle at offset {ad.get('offsetFromCurrentBC')}")
                ts = bcs[i][0] if fn.startswith("formattedBillStart") else bcs[i][1] - 86400
                self.vars[name] = java_date(ts, ad["formatString"])
            elif fn == "billCyclesBillingCost@today":
                self.vars[name] = self.bill_cycle_cost(ad)
            elif fn in ("billCostChange", "billCostPercentageChange"):
                self.vars[name] = self.bill_cost_change(fn, ad)
            elif fn in ("totalCostChange", "totalCostPercentageChange"):
                self.vars[name] = self.total_cost_change(fn, ad)
            else:
                log(f"defineVariables {name}: generateFunction {fn} not supported, left unset")
                continue
            log(f"var {name} = {self.vars[name]!r}")

    # ------------------------------------------------------------------ invoice formatter
    def s_build_invoice(self, cfg) -> None:
        """INVOICE for the user's last N completed billing cycles, priced per SMB plan (STANDARD / TOU / DEMAND, gas, BB)."""
        for c in as_list(cfg):
            c = {"customerId": "customerId", "contractId": "contractId", "premiseId": "premiseId",
                 "dataStreamIdElectric": "dataStreamIdElectric", "dataStreamIdGas": "dataStreamIdGas",
                 "meterReadingCycleCode": "SMBQA1", "numberOfCycles": 13, "electric": True, "rawDataFile": "rawToUpload",
                 "electricPlan": "STANDARD", "electricRate": 0.1180, "electricFixed": 25.00, "onPeakRate": 0.2200,
                 "offPeakRate": 0.0900, "onPeakStartHour": 12, "onPeakEndHour": 18, "demandEnergyRate": 0.0950,
                 "demandRatePerKw": 9.50, "budgetBilling": False, "bbElapsedCycles": 6, "gas": True,
                 "gasDataStreamType": "NSM", "gasSeedFile": None, "gasRawDataFile": None, "gasRatePerTherm": 1.00,
                 "gasFixed": 15.00, **c}
            cycles = self.completed_cycles("ELECTRIC" if c["electric"] else "GAS", int(c["numberOfCycles"]))
            cust, acct, prem = (str(self.lookup(c[k])) for k in ("customerId", "contractId", "premiseId"))
            out: list[str] = []
            if c["electric"]:
                iv = read_raw(self.data_path(c["rawDataFile"]))
                out += self.electric_rows(c, cycles, iv, cust, acct, prem, str(self.lookup(c["dataStreamIdElectric"])))
            if c["gas"]:
                giv = read_raw(self.data_path(c["gasRawDataFile"])) if c["gasRawDataFile"] else None
                seed = None if giv is not None else read_seed(self.data_path(c["gasSeedFile"])[0])
                out += self.gas_rows(c, cycles, giv, seed, cust, acct, prem, str(self.lookup(c["dataStreamIdGas"])))
            f = tmpfile("INVOICE_SMB_", ".txt")
            f.write_text("".join(out))
            self.vars[c["name"]] = str(f)
            log(f"invoice: {len(cycles)} cycles {date_of(cycles[0][0])}..{date_of(cycles[-1][1])}, {len(out)} rows")

    def completed_cycles(self, fuel: str, count: int) -> list[tuple[int, int]]:
        deadline = time.time() + 600
        while True:
            now = int(time.time())
            b = self.get(f"billingdata/users/{self.uuid}/homes/{HOME}/billingcycles?t0={now - 460 * 86400}&t1={now}"
                         f"&measurementType={fuel}")
            done = sorted((int(e["key"]), int(e["value"])) for e in (b if isinstance(b, list) else [])
                          if e.get("key") is not None and e.get("value") is not None and int(e["value"]) <= now)
            if len(done) >= count:
                return done[-count:]
            if time.time() >= deadline:
                if not done:
                    first = datetime.now(TZ).date().replace(day=1)
                    months = [add_months(first, -i) for i in range(count, 0, -1)]
                    return [(midnight(m), midnight(add_months(m, 1))) for m in months]
                return done
            time.sleep(30)

    def electric_rows(self, c, cycles, iv, cust, acct, prem, meter) -> list[str]:
        plan = c["electricPlan"].upper()
        rows, totals = [], []
        for s, e in cycles:
            kwh = on = mx = 0.0
            for st, val in iv:
                if s <= st < e:
                    kwh += val
                    mx = max(mx, val)
                    lt = datetime.fromtimestamp(st, TZ)
                    if lt.weekday() < 5 and c["onPeakStartHour"] <= lt.hour < c["onPeakEndHour"]:
                        on += val
            kwh = jround(kwh, 3)
            pre = prefix(cust, acct, prem, meter, "ELECTRIC", c["meterReadingCycleCode"], s, e)
            fx = c["electricFixed"]
            if plan == "TOU":
                off, on = jround(kwh - on, 3), jround(on, 3)
                oc, fc = jround(on * c["onPeakRate"], 2), jround(off * c["offPeakRate"], 2)
                total = jround(oc + fc + fx, 2)
                rows += [(pre, "TOTAL", "TOTAL", f3(kwh), f2(total)), (pre, "TOU_PK", "CONSUMPTION_BASED", f3(on), f2(oc)),
                         (pre, "TOU_OP", "CONSUMPTION_BASED", f3(off), f2(fc)), (pre, "CUSTOMER_CHARGE", "FIXED", "0.000", f2(fx))]
                self.vars.update(lastOnPeakKwh=f3(on), lastOnPeakCost=f2(oc), lastOffPeakKwh=f3(off),
                                 lastOffPeakCost=f2(fc), lastElectricBillEnergy=f2(jround(oc + fc, 2)))
            elif plan == "DEMAND":
                en = jround(kwh * c["demandEnergyRate"], 2)
                kw = jround(mx * 4, 4)
                dm = jround(kw * c["demandRatePerKw"], 2)
                total = jround(en + fx + dm, 2)
                rows += [(pre, "TOTAL", "TOTAL", f3(kwh), f2(total)), (pre, "ENERGY_CHARGE", "CONSUMPTION_BASED", f3(kwh), f2(en)),
                         (pre, "CUSTOMER_CHARGE", "FIXED", "0.000", f2(fx)), (pre, "DEMAND_CHARGE", "DEMAND_BASED", f"{kw:.4f}", f2(dm))]
                self.vars.update(lastPeakDemandKw=f"{kw:.4f}", lastDemandCost=f2(dm), lastElectricBillEnergy=f2(en))
            else:
                en = jround(kwh * c["electricRate"], 2)
                total = jround(en + fx, 2)
                rows += [(pre, "TOTAL", "TOTAL", f3(kwh), f2(total)), (pre, "ENERGY_CHARGE", "CONSUMPTION_BASED", f3(kwh), f2(en)),
                         (pre, "CUSTOMER_CHARGE", "FIXED", "0.000", f2(fx))]
                self.vars["lastElectricBillEnergy"] = f2(en)
            totals.append(total)
            self.vars.update(lastElectricBillStart=date_of(s).isoformat(), lastElectricBillTotal=f2(total),
                             lastElectricBillFixed=f2(fx), lastElectricBillKwh=f3(kwh))
        out = [row(*r, "AMI") for r in rows]
        n = int(c["bbElapsedCycles"])
        if c["budgetBilling"] and len(cycles) >= n:
            fr = len(cycles) - n
            bb = max(10.0, math.ceil(sum(totals[fr:]) / n * 1.10 / 5) * 5)
            for s, e in cycles[fr:]:
                out.append(row(prefix(cust, acct, prem, meter, "ELECTRIC", c["meterReadingCycleCode"], s, e),
                               "BUDGET_BILLING_ADJUSTMENT", "BB_AMOUNT", "0.000", f2(bb), "AMI"))
            self.vars.update(bbAmount=f2(bb), bbStartDate=date_of(cycles[fr][0]).isoformat())
        return out

    def gas_rows(self, c, cycles, iv, seed, cust, acct, prem, meter) -> list[str]:
        out = []
        for s, e in cycles:
            st = date_of(s)
            days = (date_of(e) - st).days
            if iv is not None:
                th = jround(sum(v for t, v in iv if s <= t < e), 1)
            else:
                if st.month not in seed:
                    raise StepFailed(f"gas seed has no therms/day for month {st.month}")
                th = jround(seed[st.month] * days, 1)
            en = jround(th * c["gasRatePerTherm"], 2)
            total = jround(en + c["gasFixed"], 2)
            pre = prefix(cust, acct, prem, meter, "GAS", c["meterReadingCycleCode"], s, e)
            t = f"{th:.1f}"
            out += [row(pre, "TOTAL", "TOTAL", t, f2(total), c["gasDataStreamType"]),
                    row(pre, "GAS_DELIVERY_CHARGE", "CONSUMPTION_BASED", t, f2(en), c["gasDataStreamType"]),
                    row(pre, "GAS_CUSTOMER_CHARGE", "FIXED", "0.000", f2(c["gasFixed"]), c["gasDataStreamType"])]
            self.vars.update(lastGasBillStart=st.isoformat(), lastGasBillTotal=f2(total), lastGasBillEnergy=f2(en),
                             lastGasBillFixed=f2(c["gasFixed"]), lastGasBillTherms=t)
        return out

    # ------------------------------------------------------------------ triggers
    def s_run_aggregations(self, cfg) -> None:
        """POST billingdata/.../run/aggregations for each endpoint (ELECTRIC) or the given fuel."""
        cfg = cfg if isinstance(cfg, dict) else {}
        for fuel in [x.strip() for x in str(cfg.get("measurementType", "ELECTRIC")).split(",")]:
            self.run_aggregations(fuel)

    def run_aggregations(self, fuel: str) -> None:
        if fuel.upper() == "ELECTRIC":
            eps = self.get(f"v2.0/users/{self.uuid}/endpoints") or {}
            eps = eps.get("payload", eps) if isinstance(eps, dict) else eps
            for ep in eps if isinstance(eps, list) else []:
                mt = str(ep.get("measurementType", "")).lower()
                t = "ELECTRIC" if "electric" in mt else "GAS" if "gas" in mt else "WATER"
                r = self.post(f"billingdata/users/{self.uuid}/homes/{HOME}/run/aggregations?measurementType={t}", {}, allow=(400, 500))
                log(f"run/aggregations {t}: HTTP {r['status']}")
            time.sleep(180)
        else:
            r = self.post(f"billingdata/users/{self.uuid}/homes/{HOME}/run/aggregations?measurementType={fuel.upper()}", {},
                          allow=(400, 500))
            log(f"run/aggregations {fuel.upper()}: HTTP {r['status']}")

    def s_call_modified(self, cfg) -> None:
        for c in as_list(cfg):
            c = c if isinstance(c, dict) else {}
            q = "&bypassTrueDisagg=true" if c.get("bypassTrueDisagg") else ""
            self.get(f"meta/users/{self.vars.get(c.get('userUUID', 'userUUID'), self.uuid)}/homes/{HOME}/modified?urgent=true{q}")

    def s_set_budget_from_projection(self, cfg: dict) -> None:
        pct = float(cfg.get("percentageValue", 0.75))
        bp = self.bill_projection("ELECTRIC")
        retry = 0
        while float(bp.get("currentPrice") or 0) == 0 and retry < 3:
            if retry == 0:
                self.run_aggregations("ELECTRIC")
            time.sleep(60)
            bp = self.bill_projection("ELECTRIC")
            retry += 1
        cur = float(bp.get("currentPrice") or 0)
        th = cur / pct - 2 if cur > 0 else -1
        self.post(f"meta/users/{self.uuid}/homes/{HOME}",
                  {"budgetThresholdAmount": f"{th:.2f}", "electricBudgetThresholdAmount": f"{th:.2f}"})
        log(f"budget threshold {th:.2f} (currentPrice {cur} / {pct} - 2)")
        if th < 0:
            raise StepFailed("budget threshold < 0: no current price in the open cycle")

    def bill_projection(self, fuel: str) -> dict:
        if fuel.upper() == "GAS":
            last = self.label_ts("GAS").get("last") or 0
            b = self.get(f"2.1/users/{self.uuid}/homes/{HOME}/billprojections?measurementType=GAS&time={int(last) + 1}")
        else:
            b = self.get(f"2.1/users/{self.uuid}/homes/{HOME}/billprojections")
        return (b.get("payload", b) if isinstance(b, dict) else {}) or {}

    # ------------------------------------------------------------------ email fetch
    def s_wait_email(self, cfg: dict) -> None:
        """Poll 2.1/utility_notifications for the email (DELIVERED, then GENERATED) until the subject matches."""
        subject = subst(cfg["subject"], self.vars).replace("\\Q", "").replace("\\E", "")
        ntype = cfg["notificationType"]
        deadline = time.time() + int(cfg.get("maxWaitMinutes", 8)) * 60
        start = int(self.vars[cfg.get("messageSentAfter", "messageSentAfter")])
        found = None
        for i in range(int(cfg.get("retryCount", 5))):
            if time.time() >= deadline:
                break
            if i == 1 and cfg.get("triggerAggregations", True):
                for fuel in str(cfg.get("measurementType", "ELECTRIC")).split(","):
                    self.run_aggregations(fuel.strip())
                time.sleep(200)
            lst = []
            for st in ("DELIVERED", "GENERATED"):
                b = self.get(f"2.1/utility_notifications/fetch-notifications-for-user?userID={self.uuid}&startTime={start}"
                             f"&endTime={start + 6000000}&notificationStatus={st}&eventName={ntype}") or {}
                lst = ((b.get("payload") or {}) if isinstance(b, dict) else {}).get("userNotificationStatusBeanList") or []
                if lst:
                    break
            emails = [n for n in lst if n.get("deliveryMode") == "Email" and n.get("notificationType") == ntype]
            if len(emails) > 1 and not cfg.get("isDuelFuel"):
                raise StepFailed(f"{len(emails)} {ntype} Email notifications for the user (one expected)")
            found = next((n for n in lst if subject in str(n.get("notificationTitle", ""))), None)
            if found:
                break
            log(f"waiting for {ntype} '{subject}' (attempt {i + 1}, {len(lst)} notification(s) so far)")
            time.sleep(60)
        if not found:
            raise StepFailed(f"no {ntype} email with subject containing '{subject}' within {cfg.get('maxWaitMinutes')} min")
        b = self.get(f"2.1/utility_notifications/notifications/{found['notificationId']}") or {}
        p = b.get("payload", b)
        body = p.get("notificationBody") or ""
        self.vars["emailDetails"] = {"subject": p.get("notificationTitle"), "content": body, "to": self.vars.get("email")}
        self.vars["notificationId"] = found["notificationId"]
        self.vars["_event"] = ntype
        self.vars["_eventFuel"] = found.get("measurementType") or self.vars.get("measurementType") or "ELECTRIC"
        self.vars[cfg.get("name", "emailLinks")] = [h.split("?")[0] for h in re.findall(r'<a[^>]+href="([^"]+)"', body)]
        self.vars["_title"] = p.get("notificationTitle") or ""
        self.vars["_status"] = found.get("status") or found.get("notificationStatus") or ""
        (self.run_dir / "email").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "email" / f"{ntype}.html").write_text(body)
        log(f"EMAIL {ntype} '{p.get('notificationTitle')}' id={found['notificationId']} status={found.get('status') or found.get('notificationStatus')}")

    # ------------------------------------------------------------------ data feeds for expectations





    def bill_cycle_cost(self, ad: dict) -> str:
        fuel = ad.get("measurementType", "ELECTRIC").upper()
        start = midnight(datetime.strptime(str(self.vars[ad["billCycleStartDate"]]), "%Y-%m-%d").date())
        lt = self.label_ts(fuel)
        cost = None
        for attempt in range(3):
            b = self.get(f"billingdata/users/{self.uuid}/homes/{HOME}/consumption/ctypes/billing_cost?t0={lt.get('first')}"
                         f"&t1={lt.get('last')}&mode=month&measurementType={fuel}") or {}
            v = b.get(str(start)) if isinstance(b, dict) else None
            if isinstance(v, dict) and v.get("cost") is not None:
                cost = float(v["cost"])
                break
            if attempt == 1:
                self.run_aggregations("ELECTRIC")
            time.sleep(60)
        if cost is None:
            raise StepFailed(f"no billing_cost bucket for {self.vars[ad['billCycleStartDate']]}")
        places = self.cost_decimal_places()
        txt = f"{cost:,.2f}"
        if places == 0:
            txt = re.sub(r"\.\d+", "", txt)
        else:
            txt = f"{cost:,.{places}f}"
        return "$" + re.sub(r"(?<![\d,.])0+(\d)", r"\1", txt)

    def cost_decimal_places(self) -> int:
        if "_costPlaces" not in self.vars:
            b = self.get(f"entities/pilot/{T.pilot}/configs/email_monthly_summary_section_configs") or {}
            try:
                kvs = json.loads(b["email_monthly_summary_section_configs"])["kvs"]
                v = next((k["val"] for k in kvs if k["key"] == "default_cost_decimal_places"), "0")
                self.vars["_costPlaces"] = int(v or 0)
            except Exception:       # noqa: BLE001 - default 0 decimal places
                self.vars["_costPlaces"] = 0
        return self.vars["_costPlaces"]

    def bill_cost_change(self, fn: str, ad: dict) -> str:
        lt = self.label_ts(ad.get("measurementType", "ELECTRIC"))
        b = self.get(f"billingdata/users/{self.uuid}/homes/{HOME}/utilitydata?t0={lt.get('first')}&t1={int(lt.get('last') or 0) + 2}") or {}
        rows = sorted((int(v["billingStartTs"]), float(v["cost"])) for v in (b.values() if isinstance(b, dict) else [])
                      if isinstance(v, dict) and v.get("billingStartTs") is not None and v.get("cost") is not None
                      and not v.get("empty") and not v.get("bidgelyGeneratedInvoice"))
        if len(rows) < 2:
            raise StepFailed(f"{fn}: fewer than 2 utility bills")
        prev, cur = rows[-2][1], rows[-1][1]
        delta = abs(cur - prev)
        if not (delta >= 2.0 and prev > 0):
            return ""
        if fn == "billCostChange":
            txt = f"${int(round(delta)) if round(delta) % 1 == 0 else round(delta)}"
        else:
            p = int(math.floor(delta * 100 / prev + 0.5))
            txt = f"{'+' if cur > prev else '-'}{p}% vs. previous period" if p != 0 else ""
        return txt + ad.get("suffix", "") if txt else ""

    def total_cost_change(self, fn: str, ad: dict) -> str:
        fuel = ad.get("measurementType", "ELECTRIC").upper()
        bcs = self.billing_cycles(fuel)
        lt = self.label_ts(fuel)
        m = self.get(f"billingdata/users/{self.uuid}/homes/{HOME}/consumption/ctypes/billing_cost?t0={lt.get('first')}"
                     f"&t1={lt.get('last')}&mode=month&measurementType={fuel}") or {}
        li = len(bcs) - 1 if ad.get("meterType") == "NSM" else len(bcs) - 2
        cost = lambda i: float((m.get(str(bcs[i][0])) or {}).get("cost") or 0)
        last, prev = cost(li), cost(li - 1)
        if fn == "totalCostChange":
            d = int(abs(math.floor(last - prev + 0.5)))
            return f"${d}" if d > 2 else ""
        if self.vars.get("totalCostDiffText") == "":
            return ""
        pct = int(math.floor((last - prev) * 100 / prev + 0.5)) if prev else 0
        if pct > 1:
            return f"+{pct}% vs. previous period"
        if pct <= -1:
            return f"{pct}% vs. previous period"
        return "1% vs. previous period" if pct == 1 else ""

    # ------------------------------------------------------------------ validations



    # ------------------------------------------------------------------ validation (platform APIs)
    def validate(self) -> None:
        u = self.uuid
        meta = self.get(f"meta/users/{u}") or {}
        meta = meta.get("payload", meta)
        home = self.get(f"meta/users/{u}/homes/{HOME}") or {}
        home = home.get("payload", home)
        toks = []
        gws = self.get(f"meta/users/{u}/homes/{HOME}/gws/") or {}          # {"/users/<u>/homes/1/gws/<n>": {...}}
        for path in (gws if isinstance(gws, dict) else {}):
            meters = self.get(f"meta/users/{u}/homes/{HOME}/gws/{path.rstrip('/').split('/')[-1]}/meters/") or {}
            toks += [m["token"] for m in (meters.values() if isinstance(meters, dict) else []) if isinstance(m, dict) and m.get("token")]
        want = [(self.vars.get(k), t) for k, t in (("dataStreamIdElectric", "AMI"), ("dataStreamIdGas", "NSM"))
                if self.vars.get(k)] or [(self.vars.get("dataStreamId"), self.vars.get("dataStreamType", "AMI"))]
        have = [ds for ds, t in want if any(tok.endswith(f"_{ds}_{t}") for tok in toks)]
        plan_ok = home.get("ratePlanId") == self.vars.get("ratePlanId") or self.key == "tou_onboarding"
        ok = (str(home.get("userSegment")).upper() == T.segment and len(have) == len(want) and plan_ok
              and str(meta.get("status", "ENABLED")).upper() == "ENABLED")
        self.check("C01", "user: SMB segment, enabled, meters and plan as created", "PASS" if ok else "FAIL",
                   f"segment={home.get('userSegment')} status={meta.get('status')} plan={home.get('ratePlanId')} "
                   f"meters {len(have)}/{len(want)} tokens={toks}", {"meta": meta, "home": home, "tokens": toks})
        raw_fuels = sorted({f for f in ("ELECTRIC", "GAS") if any(
            ("RAW_D_900" in n) for n in self.uploads) and (f == "ELECTRIC" and self.vars.get("serviceType", "ELECTRIC") == "ELECTRIC"
                                                           or f == "GAS" and self.vars.get("serviceType") == "GAS"
                                                           and self.vars.get("dataStreamType") == "AMI")})
        for f in raw_fuels:
            lt = self.label_ts(f)
            ok = bool(lt.get("last")) and int(lt["last"]) >= time.time() - 4 * 86400
            self.check("C02", f"{f} interval data ingested up to the last days", "PASS" if ok else "FAIL",
                       f"labelTimestamps {f}: first={lt.get('first')} last={lt.get('last')}", lt)
        for fuel, total_key in (("ELECTRIC", "lastElectricBillTotal"), ("GAS", "lastGasBillTotal")):
            if not self.vars.get(total_key):
                continue
            rows = self.invoice_rows(fuel)
            b = self.get(f"billingdata/users/{u}/homes/{HOME}/utilitydata?t0={min(rows) - 86400}&t1={int(time.time()) + 86400}"
                         f"&measurementType={fuel}") if rows else {}
            got = {date_of(int(float(v.get("billingStartTs") or k))).isoformat(): float(
                (v.get("gasCost") if fuel == "GAS" and v.get("gasCost") is not None else v.get("cost")) or 0)
                for k, v in (b.items() if isinstance(b, dict) else []) if isinstance(v, dict) and not v.get("bidgelyGeneratedInvoice")}
            match = sum(1 for s, tot in rows.items() if abs(got.get(date_of(s).isoformat(), -1) - tot) < 0.02)
            self.check("C03", f"{fuel} invoices on the platform = generated invoices", "PASS" if match == len(rows) else "FAIL",
                       f"{match}/{len(rows)} cycles match the generated TOTAL", {"generated": {date_of(k).isoformat(): v for k, v in rows.items()},
                                                                                  "platform": got})
        email = self.flow.get("email")
        if not email:
            return
        body = (self.vars.get("emailDetails") or {}).get("content") or ""
        title = self.vars.get("_title") or ""
        if not body:
            self.check("E01", f"{email['type']} email stored", "FAIL", "no email found for the user")
            return
        self.check("E01", f"{email['type']} email stored", "PASS" if str(self.vars.get("_status")).upper() in
                   ("GENERATED", "DELIVERED", "SENT") else "WARN", f"id {self.vars.get('notificationId')} status {self.vars.get('_status')}")
        self.check("E02", "subject", "PASS" if subst(email["subject"], self.vars) in title else "FAIL",
                   f"'{title}' (expected to contain '{email['subject']}')")
        text = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", body)))
        bad = re.findall(r"\$!?\{[^}]+\}|#if\b|#set\b|\bnull\b|\bNaN\b|\bundefined\b", text)
        self.check("E03", "email fully rendered (no template variables, null, NaN)", "FAIL" if bad else "PASS",
                   f"found {sorted(set(bad))[:5]}" if bad else f"{len(text)} chars of text")
        self.section_audit(body)
        getattr(self, f"v_{email['type'].lower()}", lambda t: None)(text)

    def invoice_rows(self, fuel: str) -> dict[int, float]:
        """TOTAL rows of the invoice files this run uploaded: cycle start epoch -> $ total."""
        out = {}
        for p in (self.run_dir / "input_files").glob("INVOICE_*"):
            for line in p.read_text().splitlines():
                e = line.split("|")
                if len(e) > 12 and e[4] == fuel and e[9] == "TOTAL":
                    out[midnight(date.fromisoformat(e[6]))] = float(e[12])
        return out

    def money_in(self, text: str, value: float, tol: float = 1.0) -> bool:
        return any(abs(float(x.replace(",", "")) - value) <= tol for x in re.findall(r"\$\s?([\d,]+(?:\.\d+)?)", text))

    def last_cycle_cost(self, fuel: str) -> tuple[float | None, tuple[int, int] | None]:
        now = int(time.time())
        bcs = [c for c in self.billing_cycles(fuel) if c[1] <= now]
        if not bcs:
            return None, None
        lt = self.label_ts(fuel)
        m = self.get(f"billingdata/users/{self.uuid}/homes/{HOME}/consumption/ctypes/billing_cost?t0={lt.get('first')}"
                     f"&t1={lt.get('last')}&mode=month&measurementType={fuel}") or {}
        v = m.get(str(bcs[-1][0])) if isinstance(m, dict) else None
        return (float(v["cost"]) if isinstance(v, dict) and v.get("cost") is not None else None), bcs[-1]

    def v_monthly_summary(self, text: str) -> None:
        fuel = "GAS" if self.vars.get("serviceType") == "GAS" else "ELECTRIC"
        cost, cyc = self.last_cycle_cost(fuel)
        self.check("M01", f"bill amount = platform billing_cost of the last {fuel} cycle", "PASS" if cost is not None and
                   self.money_in(text, cost) else "FAIL", f"platform ${cost} for cycle {cyc and date_of(cyc[0])}")
        if cyc:
            s, e = java_date(cyc[0], "MM/dd/yy"), java_date(cyc[1] - 86400, "MM/dd/yy")
            self.check("M02", "billing period shown", "PASS" if s in text and e in text else "WARN", f"{s} - {e}")
        if self.vars.get("lastDemandCost"):
            self.check("M03", "demand charge = invoiced demand cost", "PASS" if self.money_in(text, float(self.vars["lastDemandCost"]))
                       else "FAIL", f"${self.vars['lastDemandCost']} for peak {self.vars.get('lastPeakDemandKw')} kW")
        if self.vars.get("bbAmount"):
            self.check("M04", "budget billing amount shown", "PASS" if self.money_in(text, float(self.vars["bbAmount"]))
                       else "FAIL", f"${self.vars['bbAmount']}")
        if self.vars.get("lastOnPeakKwh"):
            self.check("M05", "TOU usage content (peak hours)", "PASS" if re.search(r"peak", text, re.I) else "FAIL",
                       "on-peak / off-peak wording")

    def v_bill_projection(self, text: str) -> None:
        fuel = "GAS" if self.vars.get("serviceType") == "GAS" else "ELECTRIC"
        bp = self.bill_projection(fuel)
        cur, proj = round(float(bp.get("currentPrice") or 0)), round(float(bp.get("projectionPrice") or 0))
        self.check("B01", "spent so far = billprojections currentPrice", "PASS" if self.money_in(text, cur) else "FAIL", f"${cur}", bp)
        self.check("B02", "projected = billprojections projectionPrice", "PASS" if self.money_in(text, proj) else "FAIL", f"${proj}")
        n = int(bp.get("daysLeft") or 0)
        self.check("B03", "days left in cycle", "PASS" if re.search(rf"\b{n} Days?\b", text) else "WARN", f"{n} days")

    def v_budget_alert(self, text: str) -> None:
        fuel = "GAS" if self.vars.get("serviceType") == "GAS" else "ELECTRIC"
        home = self.get(f"meta/users/{self.uuid}/homes/{HOME}") or {}
        home = home.get("payload", home)
        budget = float(home.get("gasBudgetThresholdAmount" if fuel == "GAS" else "electricBudgetThresholdAmount")
                       or home.get("budgetThresholdAmount") or 0)
        bp = self.bill_projection(fuel)
        cur = round(float(bp.get("currentPrice") or 0))
        self.check("A01", "75% threshold wording", "PASS" if "75%" in text else "FAIL", "75%")
        self.check("A02", "budget = home budget threshold", "PASS" if self.money_in(text, round(budget)) else "FAIL", f"${round(budget)}")
        self.check("A03", "spent = billprojections currentPrice", "PASS" if self.money_in(text, cur) else "WARN", f"${cur}", bp)

    def v_user_welcome(self, text: str) -> None:
        name = f"{self.vars.get('firstName', '')} {self.vars.get('lastName', '')}".strip()
        self.check("W01", "greeting with the account name", "PASS" if name.lower() in text.lower() else "FAIL", name)
        self.check("W02", "web portal link", "PASS" if T.dashboard_url.split("//")[-1] in
                   (self.vars.get("emailDetails") or {}).get("content", "") else "WARN", T.dashboard_url)

    def v_tou_onboarding(self, text: str) -> None:
        home = self.get(f"meta/users/{self.uuid}/homes/{HOME}") or {}
        home = home.get("payload", home)
        self.check("T01", "home moved to the TOU plan", "PASS" if "TOU" in str(home.get("ratePlanId")) else "FAIL",
                   f"ratePlanId={home.get('ratePlanId')}")
        self.check("T02", "TOU peak window shown", "PASS" if re.search(r"12 PM", text) else "FAIL", "12 PM - 6 PM weekdays")

    def v_neighbourhood_comparison(self, text: str) -> None:
        self.check("N01", "comparison amounts shown", "PASS" if re.search(r"\$\s?\d", text) and re.search(r"similar", text, re.I)
                   else "FAIL", "you / similar businesses amounts")
        if self.vars.get("dataStreamIdGas"):
            self.check("N02", "combined electricity and gas comparison", "PASS" if re.search(r"gas", text, re.I) and
                       re.search(r"electric", text, re.I) else "FAIL", "both fuels")

    def section_audit(self, body: str) -> None:
        """Every section of the user's resolved email_template_sections_config for this email (written by the
        flow's user_configs step, else the pilot/DCX list) must render in the HTML: by element id (aliases below),
        TEASER is the hidden preheader, conditional sections only render when their data exists (WARN)."""
        event, fuel = self.vars.get("_event"), str(self.vars.get("_eventFuel", "ELECTRIC")).upper()
        if not event:
            return
        b = self.get(f"entities/user/{self.uuid}/configs/email_template_sections_config?context={event}&measurement-type={fuel}") or {}
        try:
            kvs = {k["key"]: k for k in json.loads(b["email_template_sections_config"])["kvs"]}
        except Exception:       # noqa: BLE001
            kvs = {}
        dual = "dataStreamIdGas" in self.vars and event == "NEIGHBOURHOOD_COMPARISON"
        utype = str(self.vars.get("notificationUserType") or "OPT_OUT")
        order = [f"{event}.DF_EMAILS.{utype}", f"{event}.{utype}"] if dual else [f"{event}.{utype}", f"{event}.OPT_OUT", f"{event}.OPT_IN"]
        key = next((k for k in order if k in kvs and kvs[k].get("val")), None)
        if not key:
            self.check("E04", "every configured section rendered", "WARN", f"no email_template_sections_config for {event} {fuel}")
            return
        sections = [x["emailTemplateSection"] for x in json.loads(kvs[key]["val"])]
        ids = set(re.findall(r'id="([^"]+)"', body))
        missing, warn = [], []
        for sec in sections:
            if sec in HIDDEN_SECTIONS:
                continue
            if any(any(i.endswith(a) for i in ids) for a in SECTION_IDS.get(sec, (sec,))):
                continue
            if sec in TEXT_SECTIONS and re.search(TEXT_SECTIONS[sec], re.sub(r"<[^>]+>", " ", body), re.I):
                continue
            (warn if sec in CONDITIONAL_SECTIONS else missing).append(sec)
        src = f"{key} [{kvs[key].get('configSource')}]: {', '.join(sections)}"
        self.check("E04", f"every configured section rendered ({len(sections)})", "FAIL" if missing else "PASS",
                   src + (f" | missing {missing}" if missing else "") + (f" | conditional, no data: {warn}" if warn else ""))


# section enum -> rendered element ids (88004 SMB templates, verified on real emails 2026-10-27)
SECTION_IDS = {"CTA_WITH_DES_SECTION_V2": ("TOU_INTERACTION-CTA_BUTTON", "CTA_WITH_DES_SECTION_V2"),
               "CTA": ("SINGLE_BUTTON", "CTA", "CTA_BUTTON"), "SINGLE_BUTTON": ("SINGLE_BUTTON",),
               "BB_CUMULATIVE_BALANCE": ("BUDGET_BILL_CUMULATIVE_BALANCE", "BB_CUMULATIVE_BALANCE"),
               "ITEMIZATION_V2": ("ITEMIZATION_V2", "ITEMIZATION_MULTI_FUEL"), "ITEMIZATION": ("ITEMIZATION", "ITEMIZATION_MULTI_FUEL"),
               "DF_SHC_BARGRAPH_COMPARISON": ("DF_SHC_BARGRAPH_COMPARISON", "SHC_BARGRAPH_COMPARISON"),
               "SBC_BARGRAPH_COMPARISON": ("SBC_BARGRAPH_COMPARISON", "SHC_BARGRAPH_COMPARISON"),
               "MONTHLY_SUMMARY_V2": ("MONTHLY_SUMMARY_V2", "MONTHLY_SUMMARY"),
               "TOU_ONBOARDING_WELCOME": ("WELCOME_TOU", "TOU_ONBOARDING_WELCOME"), "TOU_RATE_STRUCTURE": ("TOU_RATES", "TOU_RATE_STRUCTURE"),
               "TOU_BILL_SAVING_RECO": ("TOU_BILL_SUGGESTION", "TOU_BILL_SAVING_RECO"), "TOU_MOTIVATION": ("TOU_TIPS", "TOU_MOTIVATION"),
               "TOU_USAGE": ("TOU_USAGE", "TOU_USAGE_SOLAR"), "DEMAND_CHARGES_SECTION": ("DEMAND_CHARGE", "DEMAND_CHARGES_SECTION")}
TEXT_SECTIONS = {"MONTHLY_USAGE_HISTORY_CHART": r"Monthly Usage History", "MONTHLY_USAGE_HISTORY_CHART_V2": r"Monthly Usage History"}
HIDDEN_SECTIONS = {"TEASER"}
CONDITIONAL_SECTIONS = {"TOU_RATE_PROMOTION", "TOU_USAGE", "TOU_BILL_SAVING_RECO", "SPONSORED_RECO_V2", "NEW_SERVICE",
                        "NEW_SERVICE_V2", "ITEMIZATION_EXPLAINER", "ENVIRONMENT_IMPACT", "SBC_COMPARISON_WIDGET_V2"}


# ---------------------------------------------------------------------- helpers
def jround(v: float, places: int) -> float:
    s = 10 ** places
    return math.floor(v * s + 0.5) / s          # Java Math.round(v*s)/s


def f2(v: float) -> str:
    return f"{v:.2f}"


def f3(v: float) -> str:
    return f"{v:.3f}"


def date_of(ts: int) -> date:
    return datetime.fromtimestamp(ts, TZ).date()


def midnight(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=TZ).timestamp())


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return d.replace(year=d.year + y, month=m + 1)


def prefix(cust, acct, prem, meter, fuel, code, s, e) -> str:
    a, b = date_of(s), date_of(e)
    return "|".join([cust, acct, prem, meter, fuel, code, a.isoformat(), b.isoformat(), str((b - a).days)])


def row(pre, name, ctype, usage, cost, dstype) -> str:
    return f"{pre}|{name}|{ctype}|{usage}|{cost}|{dstype}|\n"


def read_raw(paths: list[Path]) -> list[tuple[int, float]]:
    out = []
    for p in paths:
        for l in p.read_text().splitlines():
            e = l.split("|")
            if len(e) < 6 or not e[4].strip():
                continue
            end = datetime.strptime(e[4].strip(), "%Y%m%d%H%M%S").replace(tzinfo=TZ).timestamp()
            out.append((int(end) - 900, float(e[5])))
    return out


def read_seed(p: Path) -> dict[int, float]:
    seed = {}
    for l in p.read_text().splitlines():
        if l.strip() and not l.startswith("#"):
            a, b = l.split("|")[:2]
            seed[int(a)] = float(b)
    return seed


def java_date(ts: int, fmt: str) -> str:
    """SimpleDateFormat subset used by the MP04 scenarios (yyyy, yy, MM, M, dd, d, MMM)."""
    d = datetime.fromtimestamp(int(ts), TZ)
    out = fmt
    for tok, val in (("yyyy", f"{d:%Y}"), ("yy", f"{d:%y}"), ("MMMM", f"{d:%B}"), ("MMM", f"{d:%b}"), ("MM", f"{d:%m}"),
                     ("dd", f"{d:%d}")):
        out = out.replace(tok, "\x00" + val + "\x01")
    out = re.sub(r"(?<![\x00\d])M(?![\x01\d])", str(d.month), out)
    out = re.sub(r"(?<![\x00\d])d(?![\x01\d])", str(d.day), out)
    return out.replace("\x00", "").replace("\x01", "")


def si_tolerance(exp: str, act: str) -> bool:
    norm = lambda s: re.sub(r"[+-]?\d+%", "X%", re.sub(r"[$₹€£¥][\d,]+(\.\d+)?", "CX", s))
    money = lambda s: [float(x.replace(",", "")) for x in re.findall(r"[$₹€£¥]([\d,]+(?:\.\d+)?)", s)]
    pct = lambda s: [int(x) for x in re.findall(r"([+-]?\d+)%", s)]
    if norm(exp) not in norm(act):
        return False
    ea, aa = money(exp), money(act)
    return all(any(abs(x - y) <= 1 for y in aa) for x in ea) and all(any(abs(x - y) <= 1 for y in pct(act)) for x in pct(exp))




def main(doc: str, key: str, argv: list[str] | None = None) -> int:
    """Entry point of every create_smb_*_user.py (MASTERPILOT-02 style)."""
    import argparse

    from smb_flows import FLOWS
    global LOG_FILE
    ap = argparse.ArgumentParser(allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter, description=doc)
    ap.add_argument("--email", help="recipient instead of bidgelyqa+AUT_MP04_<contract>@bidgely.com (bidgely.com only)")
    ap.add_argument("--manifest", help="manifest.json of an earlier run: same user, wait for the email again + validate")
    ap.add_argument("--phases", default="all", choices=("all", "preflight"), help="preflight = pilot readiness only")
    ap.add_argument("--set-ingestion-bucket", action="store_true",
                    help="PILOT write: point s3_pull/data_ingestion at targets launchpad_bucket (reroutes the pilot's files)")
    ap.add_argument("--ensure-pilot-config", action="store_true",
                    help="PILOT write: set the sk/pilot_setup.py configs that differ (backed up first)")
    a = ap.parse_args(argv)
    if a.email:
        os.environ["MP04_EMAIL"] = a.email
    resume = json.loads(Path(a.manifest).read_text()) if a.manifest else None
    run_dir = Path(a.manifest).parent if a.manifest else \
        ROOT / "results" / key / f"run_{os.environ.get('MP04_ENV', 'productqa')}_{datetime.now():%Y%m%d_%H%M%S}"
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    LOG_FILE = run_dir / "logs" / "run.log"
    try:
        eng = Engine(key, FLOWS[key], run_dir)
        eng.set_bucket = a.set_ingestion_bucket
        return eng.run(resume, a.ensure_pilot_config, a.phases == "preflight")
    except KeyboardInterrupt:
        return 130
