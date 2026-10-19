# RAW data quality — read before validating any chart

The two RAW files in this pack are the ones actually ingested on pilot 73969, so they faithfully reproduce the current state. But they were **hand-built** (CDG cannot emit D+R streams), and they have four measured defects that make chart and daily/hourly validation meaningless.

Numbers below are computed directly from the supplied files.

---

## Defect 1 — magnitudes are 2–3 orders of magnitude too small

| | RAW file | Invoice | Gap |
|---|---|---|---|
| DELIVERED, mean per month | **0.33 kWh** | 95–235 kWh | **~250× low** (range 137× – 7,516×) |
| RECEIVED, mean per month | **−0.27 kWh** | −9 to −23 kWh | **~60× low** |
| DELIVERED, per 15-min interval | 0.113 Wh | needs ~33 Wh | |

Full per-month breakdown in `raw_vs_invoice_gap.json`.

**This is why every raw-backed chart renders as 0.0 kWh** — the values round away at display precision.

## Defect 2 — DELIVERED contains negative values

**15,092 of 64,592 rows (23.4%)** of the DELIVERED stream are negative, ranging to −1.1532 Wh.

DELIVERED is grid *import* and cannot be negative. Either the D file carries net-metered data, or D and R were crossed during generation.

Worse: in **two months the monthly net goes negative**, so the stream bills as an export —

| Month | RAW DELIVERED total |
|---|---|
| 2025-03 | **−0.0668 kWh** |
| 2025-05 | **−0.0313 kWh** |

## Defect 3 — aggregation records 1 datapoint per hour, not 4

The **files are correct here** — 96 rows/day, confirmed 15-minute granularity, matching `raw_data_granularity=900`:

| Month | Rows (DELIVERED) | Expected (96/day) |
|---|---|---|
| 2025-01 | 2,976 | 2,976 ✓ |
| 2025-02 | 2,688 | 2,688 ✓ |
| 2025-05 | 2,976 | 2,976 ✓ |

But the aggregation API reports `consumptionDataQualityAttributes.dataPoints: 1` on every **hourly** bucket, when it should be 4. So four 15-minute readings are being collapsed to one somewhere in ingestion or aggregation — **a platform issue, not a file issue.** Worth raising separately.

## Defect 4 — RAW and invoice disagree by construction

They are two unrelated datasets that happen to share a user:

| Metric | From RAW | From invoice |
|---|---|---|
| May-2026 DELIVERED | 0.26 kWh | 153.72 kWh |
| RECEIVED as % of DELIVERED | ~−100% (varies wildly, sign-unstable) | consistently **−10%** |

The invoice has a clean, deliberate −10% export ratio every cycle. RAW does not. **The monthly numbers only look correct because they are read from the invoice** — RAW contributes nothing to them.

---

## What this means for validation

| Layer | Data source | Safe to validate? |
|---|---|---|
| Monthly / bill-cycle aggregation | Invoice | ✅ Yes — all 19 cycles reconcile |
| Monthly Summary V3 web UI | Invoice | ✅ Yes — D+R split renders correctly |
| Energy Details → Year | Invoice | ⚠️ Bars correct; Net Usage line is a known FE defect |
| Energy Details → Month / daily | RAW + daily agg | ❌ No — blocked |
| Hourly views | RAW | ❌ No — blocked |
| Energy Flow / appliances | Disagg | ❌ No — disagg disabled by config |

---

## To fix (prerequisite for closing AC 6.1)

Regenerate Jorge's RAW D+R with:

1. **Realistic magnitudes** — DELIVERED averaging ~133 Wh/hour (~33 Wh per 15-min interval) so monthly rolls up to the invoiced ~95 kWh.
2. **Non-negative DELIVERED** — zero floor, no exceptions.
3. **Consistent D:R ratio** — RECEIVED at a steady −10% of DELIVERED, matching the invoice.
4. **Realistic solar shape** — export concentrated in daylight hours, not spread flat across 24h.
5. **Raw reconciling to invoice** — monthly RAW total within a few percent of the invoiced kWh for the same cycle.

Note that fixing the files still leaves daily charts broken until the daily-aggregation stream-partition defect is fixed — the two are independent, and both gate the same set of CX items.
