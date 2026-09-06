# Power BI Build Guide

Everything needed to reproduce `powerbi/asg_airlines_dashboard.pbix` from the gold
tables. Each visual names the exact source table and column, so any number on the
dashboard can be traced back to the code that produced it.

---

## 1. Load the data

**Home → Get Data → Text/CSV**, and load these from `data/gold/`:

| File | Role in the model |
|---|---|
| `dim_flight.csv` | Flight dimension — the spine of the model |
| `dim_route.csv` | Route reference |
| `dim_date.csv` | Date table |
| `dim_passenger.csv` | Passenger dimension (already PII-safe) |
| `fact_booking.csv` | Booking fact |
| `fact_payment.csv` | Payment fact |
| `kpi_headline.csv` | Single-row card values |
| `kpi_anomalies.csv` | Anomaly detail table |

The `kpi_*` tables are pre-aggregated conveniences. Every one of them can also be
derived in DAX from the dims and facts — they exist so the dashboard loads fast and
so the numbers on screen provably match the numbers the pipeline computed.

## 2. Model relationships

In **Model view**, create these — all one-to-many, single direction, from the
dimension to the fact:

| From | To | Cardinality |
|---|---|---|
| `dim_flight[flight_id]` | `fact_booking[flight_id]` | 1 → * |
| `dim_passenger[passenger_id]` | `fact_booking[passenger_id]` | 1 → * |
| `fact_booking[booking_id]` | `fact_payment[booking_id]` | 1 → * |
| `dim_route[route]` | `dim_flight[route]` | 1 → * |
| `dim_date[date]` | `dim_flight[departure_date]` | 1 → * |

Mark `dim_date` as the date table (**Table tools → Mark as date table**, on `date`).

> **Do not create a second active path between `fact_booking` and `fact_payment`.**
> They sit at different grains — 1,000 payments belong to only 637 bookings. Revenue
> at booking level must come from `fact_booking[booking_total_amount]`, which the
> pipeline already aggregated. See `DECISIONS.md` D-016.

## 3. DAX measures

Create a blank table named `_Measures` and add:

```dax
Total Flights = DISTINCTCOUNT ( dim_flight[flight_id] )

Avg Duration (min) = AVERAGE ( dim_flight[duration_minutes] )

Avg Duration (hh:mm) =
VAR m = [Avg Duration (min)]
RETURN FORMAT ( INT ( m / 60 ), "0" ) & "h " & FORMAT ( MOD ( m, 60 ), "00" ) & "m"

Overnight Flights = CALCULATE ( [Total Flights], dim_flight[is_overnight] = TRUE )

Overnight % = DIVIDE ( [Overnight Flights], [Total Flights] )

Anomaly Flights = CALCULATE ( [Total Flights], dim_flight[is_anomaly] = TRUE )

Anomaly % = DIVIDE ( [Anomaly Flights], [Total Flights] )

Total Bookings = DISTINCTCOUNT ( fact_booking[booking_id] )

-- Denominator excludes UNKNOWN so 75 unlabelled bookings cannot dilute the rate.
-- See DECISIONS.md D-012.
Cancellation Rate =
DIVIDE (
    CALCULATE ( [Total Bookings], fact_booking[status] = "CANCELLED" ),
    CALCULATE ( [Total Bookings], fact_booking[status] <> "UNKNOWN" )
)

-- Nulls are ignored by SUM, so the 78 unusable amounts are excluded rather than
-- counted as zero. See DECISIONS.md D-013.
Total Revenue = SUM ( fact_booking[booking_total_amount] )

Revenue per Flight = DIVIDE ( [Total Revenue], [Total Flights] )

Airline Share % =
DIVIDE ( [Total Flights], CALCULATE ( [Total Flights], ALL ( dim_flight[airline] ) ) )
```

## 4. Shared slicer panel

Put the same slicers on every page so filters read consistently:

- `dim_flight[airline]` — dropdown
- `dim_flight[route]` — dropdown, searchable
- `dim_flight[departure_date]` — between
- `dim_flight[departure_time_band]` — tiles

Use **Sync slicers** across all four pages.

---

## Page 1 — Duration Analysis

| Visual | Type | Source |
|---|---|---|
| KPI cards row | 4 cards | `[Avg Duration (hh:mm)]`, `[Total Flights]`, `[Overnight Flights]`, `[Overnight %]` |
| Average duration by airline | Bar | `kpi_duration_by_airline[airline]` vs `avg_duration_minutes` |
| Duration distribution | Histogram (binned column) | `dim_flight[duration_minutes]`, 30-minute bins |
| Duration by route | Bar, top 10 | `dim_route[route]` vs `avg_duration_minutes` |
| Overnight vs same-day | Donut | `dim_flight[is_overnight]`, count of `flight_id` |
| Min / median / max by airline | Table | `kpi_duration_by_airline` |

**The story to tell:** average duration is 164.8 minutes and is almost identical
across all four carriers (163.8–165.4). 122 flights (12.2%) cross midnight and every
one has a correct positive duration.

## Page 2 — Route Performance

| Visual | Type | Source |
|---|---|---|
| Cards | 3 | `[Total Flights]`, distinct `dim_flight[route]`, `[Revenue per Flight]` |
| Traffic by route | Bar, sorted desc | `kpi_route_traffic[route]` vs `flights_operated` |
| Origin → destination matrix | Matrix, conditional fill | Rows `source`, Columns `destination`, Values count of `flight_id` |
| Airport movements | Clustered bar | `kpi_airport_traffic`: `departures` and `arrivals` by `airport` |
| Revenue by route | Bar | `kpi_route_traffic[route]` vs `revenue` |
| Route detail | Table | `kpi_route_traffic` — all columns |

**The story:** 30 routes across 6 airports. BOM is the busiest with 374 movements
(205 departures, 169 arrivals); BLR is the quietest at 297.

## Page 3 — Airline Trends

| Visual | Type | Source |
|---|---|---|
| Cards | 3 | `[Total Bookings]`, `[Total Revenue]`, `[Cancellation Rate]` |
| Flight share by airline | Donut | `kpi_duration_by_airline[airline]` vs `share_of_flights_pct` |
| Bookings by status | Stacked column | `kpi_booking_status`, `status` vs `bookings` |
| Revenue by payment method | Bar | `kpi_payment_method`: `revenue` by `payment_method` |
| Departures by hour | Line | `kpi_hourly_load`: `flights_departing` by `departure_hour` |
| Passenger age bands | Column | `kpi_passenger_demographics`, `age_band` by `gender` |

**The story:** a genuinely even four-way market split (27.1 / 25.4 / 24.6 / 22.9%).
Cancellation rate is 33.9% of bookings whose status is known.

Add a text box: *"Revenue excludes 78 payments with a missing or invalid amount;
these are counted as null, never as zero."*

## Page 4 — Delay / Anomaly Insights

| Visual | Type | Source |
|---|---|---|
| Cards | 3 | `[Anomaly Flights]`, `[Anomaly %]`, count of quarantined rows (1) |
| Anomalies by reason | Bar | `kpi_anomalies[anomaly_reason]`, count |
| Anomaly detail | Table | `kpi_anomalies` — all columns |
| Duration vs route median | Scatter | `dim_flight`: x `route_median_duration_minutes`, y `duration_minutes`, colour by `is_anomaly` |
| Data quality summary | Table | Manual: defect, rows affected, handling (from the README table) |

**Required text box on this page — the reviewer will look for it:**

> **Why there is no delay metric.** A delay is actual departure minus *scheduled*
> departure. This dataset contains one departure timestamp per flight and no
> schedule to compare it against, so a true delay cannot be computed and none has
> been invented. What is measurable is duration anomaly detection, shown here.
> Adding a `scheduled_departure_time` column to the source feed would enable genuine
> delay metrics with no change to the pipeline. See `DECISIONS.md` D-011.

---

## 5. Formatting

- One theme across all pages; airline colours consistent everywhere.
- Titles state the measure and its unit ("Average flight duration, minutes").
- Sort every bar chart by value, descending — never alphabetically.
- Cards show one decimal at most.
- Give each page a one-line subtitle saying what question it answers.

## 6. Export

Save as `powerbi/asg_airlines_dashboard.pbix`, then screenshot each page to
`powerbi/screenshots/page1_duration.png` … `page4_anomalies.png`.
