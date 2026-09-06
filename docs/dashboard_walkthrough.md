# Dashboard Walkthrough

Page-by-page description of `powerbi/asg_airlines_dashboard.pbix`, naming the measure
behind every figure so any number on screen can be traced back to the model.

Report canvas is 1920×1080. All four pages carry slicers on **airline**, **route** and
**booking month**.

---

## Page 1 — Executive Overview

The landing page. Six KPI cards across the top, three charts below.

| Visual | Type | Measure / field |
|---|---|---|
| Total flights | Card | `[Flights]` — **1,003** |
| Avg flight duration (min) | Card | `[Average Duration Minutes]` — **164.67** |
| Total bookings | Card | `[Bookings]` — **1,000** |
| Valid revenue (INR) | Card | `[Gross Valid Payment Amount]` — **₹7.39M** |
| Cancellation rate | Card | `[Cancellation Rate]` — **31.4%** |
| Payment coverage | Card | `[Payment Coverage]` — **63.7%** |
| Flights operated by airline | Bar | `dim_airline[airline]` × `[Flights]` |
| Bookings by month | Line | `dim_booking_date[year_month]` × `[Bookings]` |
| Valid revenue by month (INR) | Column | `dim_booking_date[year_month]` × `[Gross Valid Payment Amount]` |

**Payment coverage of 63.7% is the finding to lead with.** 363 of 1,000 bookings carry
no payment record at all — the single largest data quality issue in the source, and the
reason revenue is labelled *valid* rather than *total*.

## Page 2 — Duration & Schedule Analysis

Answers the transformation question the case study is really testing.

| Visual | Type | Measure / field |
|---|---|---|
| Avg flight duration (min) | Card | `[Average Duration Minutes]` — **164.67** |
| Overnight flights (cross-day) | Card | `[Overnight Share]` — **12.2%** |
| Red-eye departures | Card | `[Red Eye Share]` — **27.0%** |
| Departures by hour of day | Column | `fact_flight[departure_hour]` × `[Flights]` |
| Duration profile by route (minutes) | Matrix | `dim_route[route]` × `[Flights]`, `[Average Duration Minutes]`, `[Duration Spread Minutes]` |
| Flight-level detail | Table | `flight_id`, `departure_time`, `arrival_time`, `duration_minutes`, `correction_reason` |

The flight-level table is deliberately included: it exposes `correction_reason` per
row, so a reviewer can see exactly which records the pipeline touched and why, rather
than taking the aggregate on trust.

**Duration spread is the honest caveat.** Route-level spread runs 54–83 minutes against
route averages of 128–187, so durations vary widely *within* every route. That is why
anomaly detection uses a per-route statistical fence rather than a fixed tolerance
(`DECISIONS.md` D-009).

## Page 3 — Route & Airline Operational Performance

| Visual | Type | Measure / field |
|---|---|---|
| Traffic by route | Bar, sorted desc | `dim_route[route]` × `[Flights]` |
| Flight share by airline | Donut | `dim_airline[airline]` × `[Airline Flight Share]` |
| Route performance detail | Table | route, source, destination, `[Flights]`, `[Bookings]`, `[Gross Valid Payment Amount]`, `[Average Duration Minutes]` |

Top routes: BOM–CCU (90), CCU–DEL (72), MAA–BLR (65), BLR–BOM (60), HYD–MAA (57).
Airline share is close to even — Air India 23.2%, IndiGo 24.8%, SpiceJet 23.5%,
Vistara 21.7%, with 6.7% still labelled UNKNOWN in this model.

Table totals reconcile to 1,003 flights / 1,000 bookings / ₹73,85,142.98 / 164.67 min.

## Page 4 — Data Quality, Pipeline Audit & Governance

The governance page. It opens with the delay disclaimer.

| Visual | Type | Measure / field |
|---|---|---|
| Source issue events | Card | `[Source Issue Events]` — **714** |
| Flights corrected | Card | `[Corrected Flights]` — **1** |
| Unresolved flight bookings | Card | `[Unresolved Flight Bookings]` — **2** |
| Payments with invalid amount | Card | `[Invalid Amount Payments]` — **30** |
| Rows affected, by issue type | Bar | `dq_issue_summary[issue]` × `Sum(affected_rows)` |
| Data quality issue register | Table | `table_name`, `issue`, `treatment`, `Sum(affected_rows)` |

### The issue register — all 714 events

| Table | Issue | Treatment | Rows |
|---|---|---|---:|
| bookings | booking_without_payment | retain | 363 |
| bookings | missing_status | retain_as_unknown | 45 |
| bookings | sentinel_status | retain_as_invalid | 30 |
| bookings | unavailable_flight_reference | retain_with_unknown_flight | 2 |
| flights | conflicting_duplicate_key | quarantine | 2 |
| flights | duration_reconciliation_mismatch | retain_timestamp_duration | 1 |
| flights | exact_duplicate | exact_duplicate_removed | 15 |
| flights | flight_without_booking | retain | 20 |
| flights | missing_airline | retain_as_unknown | 41 |
| flights | negative_duration | roll_arrival_forward_one_day | 1 |
| flights | sentinel_airline | retain_as_unknown | 31 |
| passengers | duplicate_passenger_id | deterministic_survivorship | 75 |
| passengers | missing_last_name | mask_available_name | 10 |
| payments | missing_amount | retain_with_null_amount | 48 |
| payments | non_numeric_amount | retain_with_null_amount | 30 |

Publishing the treatment alongside the issue is the point of this page: it makes every
cleaning decision auditable from the dashboard, not only from the code.

`negative_duration → roll_arrival_forward_one_day` is the overnight repair described in
`DECISIONS.md` D-006 and D-007. `duration_reconciliation_mismatch` is the one row where
the recomputed duration disagreed with the source workbook's Excel formula — resolved
in favour of the timestamps, because the formula cannot see the repair.

### Why there is no delay metric

Stated on the page itself:

> Operational delay is not computable from this dataset. The source provides only actual
> departure and arrival timestamps, with no scheduled times to compare against. Duration
> reflects the flight interval. The metrics below report observable data-quality
> anomalies instead.

Adding a `scheduled_departure_time` column to the feed would enable true delay metrics
with no change to the pipeline's shape.

---

## Known limitation

`UNKNOWN` appears as a fifth airline holding 6.7% of flights. Those 72 records
(41 null + 31 sentinel) have a recoverable carrier: the `flight_id` prefix maps 1:1 and
conflict-free to airline across all 1,020 source rows (`AI`→Air India, `UK`→Vistara,
`SJ`→SpiceJet, `6F`→IndiGo). The pipeline in `src/` performs that repair
(`clean.repair_airline_from_flight_id`); this dashboard model retains the UNKNOWN
category instead. Applying the repair in the dashboard model would remove the fifth
slice and redistribute those 72 flights to their real carriers.
