# DA-1795 — MDS Pilot Config + Ingestion File Pack (for QA)

**Source pilot:** 73969 (`test-mds`) on DETO Dev → masterpilot-dev
**Test user:** Jorge Gomez · `6655e2b6-ad88-4306-8681-47d3afded472` · partnerUserId `73969_7185227854`
**Config values verified live:** 24 Aug 2026
**Files sourced from:** `s3://bidgely-masterpilot-dev-external/Incoming/` (the exact files that were ingested)

---

## Read this first

Ingestion and **monthly** aggregation on this pilot are solid — you can validate those confidently with this pack.

**The RAW files in this pack are not fit for validating charts or daily/hourly data.** They were hand-built (CDG cannot emit D+R) and carry defects that are quantified in `03-validation/DATA-QUALITY-WARNING.md`. Everything that reconciles today does so because it comes from the **invoice**, not from RAW.

So please scope your validation to:

| Validate with confidence | Do not validate yet |
|---|---|
| Ingestion success (all 6 files) | Energy Details → Month / daily charts |
| Meter + stream creation (2 meters) | Anything hourly or daily |
| Monthly / bill-cycle aggregation per stream | Energy Flow numbers (disagg is off by config) |
| Invoice → API reconciliation (19 cycles) | Appliance breakdown / pie |
| Monthly Summary V3 D+R split in web UI | Net Usage totals on the year chart (known FE defect) |

---

## 1. Folder contents

```
01-configs/          the configurations you asked about (POST-ready JSON)
02-ingestion-files/  BC Schedule, RAW (D+R), Invoice, plus the 2 enrolment files
03-validation/       expected values per bill cycle + data-quality warning
```

---

## 2. Configurations for MDS reuse

Apply with:

```bash
curl -X POST "https://<api-server>/entities/<PILOT_ID>/configs" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <TOKEN>" \
  -d @01-configs/01_launchpad_ingestion_configs_MDS_CORE.json
```

| File | What it covers | Priority |
|---|---|---|
| `01_launchpad_ingestion_configs_MDS_CORE.json` | **The 7 keys that make a pilot MDS.** This is the answer to "what's required for reuse". | Required |
| `02_launchpad_ingestion_configs_FILE_PARSING.json` | Delimiters, date formats, units, granularity, identifier formats. The supplied files will not parse without these. | Required |
| `03_field_mapping_configs.json` | Field positions. Includes `invoice_launchpad.Data_Stream_Id`, which is **MDS-mandatory** — without it the invoice cannot be tied to a stream. | Required |
| `04_disagg_and_solar_configs.json` | Disagg/solar flags, **as-is on 73969 (disagg OFF)**. See §5. | Decision needed |
| `05_frontend_configs_CX.json` | CX configs. Section A = verified present; Section B = **missing on 73969, add these**. | Required for CX |
| `frontend_configs_DELIVERED_snapshot.json` | Raw dump of all 155 FE configs actually delivered to the browser. Reference only. | Reference |

### The 7 MDS core keys, at a glance

| Key | Value | Note |
|---|---|---|
| `create_streams_based_on_supported_streams_config` | `true` | Master MDS switch |
| `supported_streams` | JSON, 4 streams | DELIVERED(""), CL1, CL2, RECEIVED("R") |
| `handle_meter_enrollment` | `true` | Needed for stream-specific meters |
| `update_streams_based_on_rate_plan` | `true` | ⚠️ **live pilot has `false`** |
| `handleRateTransition` | `true` | |
| `enable_same_meter_updation` | `false` | ⚠️ **live pilot has `true`** |
| `solar_transition_invalidate_nonsolar_meter` | `false` | |

> **⚠️ Config drift to be aware of.** Two of these seven do **not** match the DA-1795 spec on pilot 73969 right now. Both are pilot-source values, so the DETO pilot template supplied a default and the MDS override was never applied. The JSON files carry the **corrected** values. If you are reproducing 73969 exactly, use the live values; if you are setting up a clean MDS pilot, use the corrected ones.

### Frontend configs missing on 73969 (add these)

| Key | Set to | Why it matters |
|---|---|---|
| `dashboard.solar_billing_type` | `invoice` | **Confirmed cause of the missing Home Usage History widget.** Unset → code routes to `NET_BILL_HISTORY`, which has no entry in `dynamic_layout_configs`, so the widget silently disappears. |
| `DASHBOARD.USAGE_SUMMARY_V2.SHOW_SOLAR_STREAM_COST` | `true` | Gates the two-stream Received/Delivered card on Home Bill Summary. Unset and the widget does not render. |
| `DASHBOARD.MONTHLY_SUMMARY.V3.SHOW_SOLAR_STREAM_COST` | `true` | MS V3 stream card. Renders anyway, but set it explicitly. |
| `web_accessibility` | `true` | Contributes to the ADA gap. Note this alone will **not** fix ADA — the Highcharts accessibility module isn't loaded at all (Web-team code fix). |
| `dashboard.disagg_enabled_mappings` | *(leave unset)* | Currently benign — falls through to the `NON_DISAGG` keys, which are set. |

These two are **untested** — the config write was blocked in the analysis session, so they are reasoned from the deployed JS bundle's own reads, not confirmed by watching the UI change. Worth applying first and re-checking Home.

### Do not skip these two

`DASHBOARD.INSIGHTS.USAGE_V2.ALLOWED_VIEWS_SOLAR` and `…NON_DISAGG.ALLOWED_VIEWS_SOLAR` are **mandatory** for a solar pilot. The bundle calls `JSON.parse()` on them directly; if either is missing the Insights page renders a blank `#root` white screen. They are already set on 73969 — just don't drop them when copying configs to a new pilot.

---

## 3. Ingestion files

Ingest in this order — each depends on the previous:

| # | File | Rows | Notes |
|---|---|---|---|
| 1 | `USERENROLL_D_20260821_73969_jorge.csv` | 1 | Creates the user. Col 35 = `True` → solar flag → drives the SOLAR web layout. |
| 2 | `METERENROLL_D_20260821_73969_JORGE_RATE_131317.csv` | 2 | **The MDS-critical file.** One row per stream: `911141002` and `911141002-R`. Both carry rate plan `MDS_SOLAR_TOU_D_R_2026`. |
| 3 | `BC_SCHEDULE_73969_4_114520.txt` | 24 | BCC 4, calendar months, 2024-09 → 2026-08. **Must load before RAW** — RAW chunks fail with `LINES_FAILED` if the bill cycle is missing. |
| 4 | `RAW_D_900_S_73969_JORGE_D_full_124013.txt` | 64,592 | DELIVERED stream, 15-min, 2024-09-23 → 2026-07-28. |
| 5 | `RAW_D_900_S_73969_JORGE_R_full_124013.txt` | 64,592 | RECEIVED stream, meter `-R` suffix, same span. Values are negative (export). |
| 6 | `INVOICE_73969_JORGE_FEEDIN_120125.txt` | 114 | 19 bill cycles × 6 line items. Carries the `FEED_IN` charge name that binds cost to the RECEIVED stream. |

**File format** — all pipe-delimited, no header row.

RAW: `customerId|contractId|premiseId|meterId|timestamp|value|?|meterType`
```
7185227854|5562650170|0196882462|911141002|20240923211500|0.5844|0|AMI
7185227854|5562650170|0196882462|911141002-R|20240923211500|-0.1753|0|AMI
```

INVOICE: `…|meterId|ELECTRIC|bcc|start|end|days|chargeName|chargeType|value|cost|meterType`
```
…|911141002|ELECTRIC|4|2025-01-01|2025-01-31|31|TOTAL|TOTAL|95.89|321.11|AMI|
…|911141002|ELECTRIC|4|2025-01-01|2025-01-31|31|FEED_IN|CONSUMPTION_BASED|-9.59|-0.48|AMI|
```

> **Note on the invoice:** there is **one invoice per bill cycle on the DELIVERED meter id only** — `911141002`, never `911141002-R`. Stream association happens through the **charge name** (`FEED_IN` → RECEIVED), per PE-13501. This is correct and intentional; don't expect a separate `-R` invoice.

---

## 4. What to assert

`03-validation/expected_values_per_bill_cycle.json` has all 19 cycles machine-readable. All 19 reconcile: `TOU_PK + TOU_OP + FEED_IN + Daily Fixed + Monthly Fixed = TOTAL`.

Spot-check cycle (May-2026, `bill-start=1780203600` `bill-end=1782795599`):

| Assertion | Expected |
|---|---|
| DELIVERED consumption | **95.89 kWh** (`95890` Wh in agg API) |
| RECEIVED consumption | **−9.59 kWh** (`−9590` Wh) |
| DELIVERED cost | **$321.59** |
| RECEIVED cost | **−$0.48** |
| Total bill | **$321.11** |
| Energy charges | **$307.63** (184.87 + 123.24 − 0.48) |
| Fixed charges | **$13.48** (8.09 + 5.39) |

Per-stream aggregation check:
```bash
B="https://api-server-masterpilot-dev.bidgely.com/billingdata/users/$U/homes/1/consumption/ctypes"
for ST in DELIVERED RECEIVED CONTROLLED_LOAD_1; do
  curl -s -H "Authorization: Bearer $TOKEN" \
    "$B/ENERGY_CONSUMPTION/stream-types/$ST?mode=month&t0=1727755200&t1=1790000000"
done
# expect: DELIVERED positive, RECEIVED negative, CONTROLLED_LOAD_1 all zeros
```

### Two known API quirks — not your bugs

1. **Bill-cycle timestamps are offset one day** from the invoice dates. Invoice says `2025-01-01..2025-01-31`; the aggregation API returns the bucket at `1735624800` = 2024-12-31. This is `parser_time_zone=America/Chicago` doing its job. Match cycles by value, not by date string.
2. **`Monthly Fixed` shows as `UNKNOWN` in `invoice-data-info`.** The API returns `FIXED: 13.48` with `costBreakdownMap {UNKNOWN: 5.39}`. Cause: `Daily Fixed` / `Monthly Fixed` are **not canonical charge names** — PE-13501 defines `MONTHLY_CONNECTION_CHARGE` and `FACILITY_CHARGE`. Totals are right; only the breakdown label is wrong. Worth fixing in the next invoice file.

---

## 5. Two decisions you'll hit

**Energy Flow will show 0/0/0 and the appliance pie will show NaN.** That is *configured*, not broken data — `disagg_preference.enable_solar=false` and `trigger_ami_disagg_for_solar_user=false` on this pilot. Two ways forward:
- turn solar disagg on (estimated generation), or
- add `SOLAR_GENERATION` (suffix `SG`) to `supported_streams` and ingest a real generation stream — the MDS-native route, and currently untried.

Until one is chosen, please don't log Energy Flow zeros as defects.

**TOU + solar (AC 6.2) is blocked.** Rate plan `MDS_SOLAR_TOU_D_R_2026` / planNumber 905 is **flat** despite the name — flat eRates, `tou=null`, `isTouRate=false`, and the API reports `touUser: false`. A real TOU MDS plan is needed.
Worth knowing: **the invoice already carries `TOU_PK` and `TOU_OP`** and those are being dropped before they reach the TOU cType, which returns empty for every stream. That's the PE-13501 invoice→TOU mapping path, untested end-to-end on DETO. Exercise it deliberately once a TOU plan exists.

---

## 6. Open items on my side

- **Config values are from 24 Aug 2026.** The API token expired since, so I could not re-pull the live blob today. Values above are from a verified read, not re-confirmed this morning — re-pull before treating them as current.
- **Config Registry (AC 7) not checked** — whether these MDS keys are registered is still open; its API sits behind the CMS backend and I had no reachable host.
- **`SHOW_SOLAR_STREAM_COST` / `solar_billing_type` are untested**, as noted in §2.

Ping me if you want the live blob re-pulled once I have a fresh token, or a walkthrough of the ingest order.
