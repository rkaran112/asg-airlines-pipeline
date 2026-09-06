# ASG Airlines — End-to-End Data Engineering Pipeline

Ingests ASG Airlines' operational flight data from a multi-sheet Excel export,
repairs the data quality defects in it, protects passenger PII, models the result as
a star schema, and publishes KPI tables for a Power BI dashboard.

**Result of the current run: 4,059 source rows in, 4,004 curated rows out, 1 row
quarantined, 54 duplicates removed. Nothing is dropped silently.**

---

## Quick start

```bash
pip install -r requirements.txt
```

```bash
python -m src.pipeline
```

```bash
python -m pytest tests -q
```

The run writes `data/gold/*.csv`, which is what Power BI reads. It takes a few
seconds and prints a full audit log.

---

## Repository map

| Path | What it is |
|---|---|
| `src/` | The pipeline. One module per stage, described below. |
| `notebooks/asg_airlines_pipeline.ipynb` | Narrated walkthrough. Calls the modules — it does not re-implement them. |
| `tests/test_pipeline.py` | 11 tests over the logic that could be silently wrong. |
| `data/raw_UseCase-Airlines.xlsx` | The source workbook as provided. |
| `data/bronze/` | Verbatim sheet extracts. Gitignored — contains raw PII. |
| `data/silver/` | Cleaned, validated, PII-protected. Gitignored (intermediate). |
| `data/gold/` | **The deliverable.** Star schema + KPI tables, CSV and Parquet. |
| `data/quarantine/` | Every rejected row, with the rule that rejected it. |
| `docs/` | Architecture, data flow, data model, and the documentation `.docx`. |
| `powerbi/` | Dashboard `.pbix` and screenshots. |
| `DECISIONS.md` | Every judgement call, why it was made, and what it costs. |

---

## Architecture

A medallion (bronze → silver → gold) lakehouse pattern, implemented locally in
Python. See `DECISIONS.md` D-001 for why local rather than Azure, and the
Scalability section below for the migration path.

```
UseCase - Airlines.xlsx
  (flights, bookings, passengers, payments)
            |
            v
   [1] INGEST      read every column as text, land verbatim   -> data/bronze/
            |
   [2] VALIDATE    schema contract; missing column stops the run
            |
   [3] CLEAN       null tokens, duplicates, standardisation,
                   airline repair, survivorship
            |
   [4] TRANSFORM   parse timestamps, overnight repair, duration,
                   route/time attributes, anomaly flags
            |
   [5] PRIVACY     hash / mask / generalise / drop PII         -> data/silver/
            |                  |
            |                  +--> rejected rows              -> data/quarantine/
            v
   [6] MODEL       star schema: 4 dimensions, 2 facts          -> data/gold/
            |
   [7] KPIS        9 pre-aggregated metric tables              -> data/gold/
            |
            v
       Power BI dashboard (4 pages)
```

---

## Execution trace

One run, top to bottom. Every line is a real call in the code.

```
python -m src.pipeline
└─ pipeline.run()                                        src/pipeline.py:27
   │
   ├─ [1] ingest.load_workbook()                         src/ingest.py:17
   │        xlsx -> {sheet: DataFrame}, all columns as str  (4,059 rows)
   │     ingest.write_bronze()                           src/ingest.py:39
   │
   ├─ [2] validate.check_schema()                        src/validate.py:30
   │        raises SchemaError on a missing column
   │
   ├─ [3] clean.clean_flights()                          src/clean.py:90
   │      ├─ _normalise_null_tokens()                    src/clean.py:23   "UNKNOWN" -> NaN
   │      ├─ validate.reject() x4                        src/validate.py:48 airport/id rules
   │      ├─ resolve_conflicting_flight_ids()            src/clean.py:65   -15 dupes, -1 conflict
   │      └─ repair_airline_from_flight_id()             src/clean.py:33   68 airlines recovered
   │     clean.clean_passengers()                        src/clean.py:150
   │      └─ resolve_duplicate_passengers()              src/clean.py:124  1039 -> 1000
   │     clean.clean_bookings()                          src/clean.py:173  75 statuses -> UNKNOWN
   │     clean.clean_payments()                          src/clean.py:209  78 amounts -> null
   │
   ├─ [4] transform.transform_flights()                  src/transform.py:172
   │      ├─ parse_timestamps()                          src/transform.py:40  format="mixed"
   │      ├─ compute_duration()                          src/transform.py:63  +1 day on 1 row
   │      ├─ crosscheck_against_source_duration()        src/transform.py:82  1003/1003 agree
   │      ├─ add_route_and_time_attributes()             src/transform.py:108
   │      └─ flag_anomalies()                            src/transform.py:121 2 anomalies
   │     validate.check_referential_integrity()          src/validate.py:65  0 orphans
   │
   ├─ [5] privacy.mask_passengers()                      src/privacy.py:84
   │     privacy.mask_bookings()                         src/privacy.py:104
   │     privacy.assert_no_raw_pii()                     src/privacy.py:116  guard rail
   │     pipeline.write_silver()                         src/pipeline.py:85
   │
   ├─ [6] model.build_star_schema()                      src/model.py:101
   │      ├─ summarise_payments_by_booking()             src/model.py:70   1000 -> 637
   │      ├─ build_fact_booking()                        src/model.py:90   asserts no fan-out
   │      ├─ build_dim_flight/route/date()               src/model.py:30/41/53
   │      └─ model.write_gold()                          src/model.py:119
   │
   ├─ [7] kpis.build_all()                               src/kpis.py:139   9 KPI tables
   │     kpis.write_all()                                src/kpis.py:159
   │
   ├─ validate.write_quarantine()                        src/validate.py:79
   └─ pipeline.report_reconciliation()                   src/pipeline.py:93
```

### What each module owns

| Module | Owns | Receives | Returns | Never does |
|---|---|---|---|---|
| `config.py` | Every path, threshold and business constant | — | — | Touch data; import another `src` module |
| `ingest.py` | Getting bytes off disk | A file path | `{sheet: DataFrame}` of strings | Clean, type or reshape anything |
| `validate.py` | Deciding if a row may continue; the quarantine ledger | A frame + a failure mask | The surviving rows | Change a value — it only splits keep/reject |
| `clean.py` | Null tokens, duplicates, casing, the airline repair | Raw string frames | Typed, de-duplicated frames | Parse timestamps or compute durations |
| `transform.py` | Every derived flight measure | A cleaned flight frame | Flights + duration, overnight, anomaly columns | Mask PII or build tables |
| `privacy.py` | Every field a regulator calls personal data | Cleaned frames | PII-safe frames | Return a raw value it was given |
| `model.py` | The shape of the gold zone | PII-safe frames | `{table_name: DataFrame}` | Clean or derive — only reshapes |
| `kpis.py` | The numbers on the dashboard | The gold tables | `{kpi_name: DataFrame}` | Recompute a derived column |
| `pipeline.py` | Stage order and the run report | — | The gold and KPI tables | Contain business logic |

### Data lineage

| Source | Becomes | Via |
|---|---|---|
| `flights.airline` (72 null/UNKNOWN) | `dim_flight.airline` + `airline_was_repaired` | `clean.repair_airline_from_flight_id` |
| `flights.departure_time`, `arrival_time` | `dim_flight.duration_minutes`, `is_overnight` | `transform.parse_timestamps` → `compute_duration` |
| `flights.duration` (Excel formula) | *dropped* — used once as a cross-check | `transform.crosscheck_against_source_duration` |
| `flights.source`, `destination` | `dim_flight.route`, `dim_route` | `transform.add_route_and_time_attributes` |
| `passengers.aadhaar_id` | `dim_passenger.aadhaar_hash` | `privacy.hash_column` |
| `passengers.email` | `email_masked` + `email_domain` | `privacy.mask_email` |
| `passengers.date_of_birth` | `dim_passenger.age_band` | `privacy.age_band` |
| `bookings.passport_number` | `fact_booking.passport_hash` | `privacy.hash_column` |
| `bookings.emergency_contact_*` | *dropped entirely* | `privacy.mask_bookings` |
| `bookings.status` (75 null/INVALID) | `fact_booking.status` = `UNKNOWN` + `status_was_unknown` | `clean.clean_bookings` |
| `payments.amount` (78 unusable) | `fact_payment.amount` (null) + `fact_booking.booking_total_amount` | `clean.clean_payments` → `model.summarise_payments_by_booking` |

---

## Data quality defects found and how each was handled

| # | Defect | Scale | Handling | Decision |
|---|---|---|---|---|
| 1 | `airline` null or `"UNKNOWN"` | 72 rows | Recovered from the `flight_id` prefix (1:1, proven conflict-free) | D-003 |
| 2 | Exact duplicate flight rows | 15 rows | Dropped | D-004 |
| 3 | Same `flight_id`, different route | `6F250` | First kept + flagged, second quarantined | D-004 |
| 4 | Two different timestamp formats | 35 cells | Parsed with `format="mixed"` — **not** quarantined | D-005 |
| 5 | Overnight (cross-day) flights | 122 flights | Full-date subtraction; verified positive | D-006 |
| 6 | Arrival dated before departure | `SJ192` | +1 day repair → 300 min, confirmed by the workbook's own formula | D-007 |
| 7 | `duration` is a live Excel formula | all rows | Never trusted; used once as an independent cross-check | D-008 |
| 8 | `status` null or `"INVALID"` | 75 rows | Labelled `UNKNOWN`, kept, excluded from the cancellation denominator | D-012 |
| 9 | `amount` blank or `"INVALID"` | 78 rows | Kept null, never zero-filled | D-013 |
| 10 | Conflicting duplicate `passenger_id` | 39 rows | Most-complete record survives | D-014 |
| 11 | Missing `last_name` | 10 rows | Blank — the field is dropped by masking anyway | D-014 |
| 12 | 363 bookings with multiple payments | 1000 → 637 | Aggregated to booking grain *before* joining | D-016 |

---

## Data model

Star schema. Both facts join to the shared dimensions; **the two facts are never
joined to each other** — that is the payment fan-out trap (D-016).

```
                    dim_date (345)
                        |
     dim_route (30) --  |  -- dim_passenger (1,000, PII-safe)
              \         |        /
               \        |       /
              dim_flight (1,004)
                 /            \
    fact_booking (1,000)   fact_payment (1,000)
         [1 row per booking]   [1 row per payment]
```

| Table | Grain | Rows |
|---|---|---|
| `dim_flight` | one flight | 1,004 |
| `dim_passenger` | one passenger (hashed / masked / banded) | 1,000 |
| `dim_route` | one source–destination pair | 30 |
| `dim_date` | one calendar date | 345 |
| `fact_booking` | one booking | 1,000 |
| `fact_payment` | one payment | 1,000 |

---

## KPI results

Nine KPI tables in `data/gold/`. Headline figures from the current run:

| Metric | Value |
|---|---|
| Total flights | 1,004 |
| Average flight duration | **164.8 min** (median 166.5) |
| Overnight flights | **122 (12.2%)** |
| Routes operated | 30, across 6 airports |
| Busiest airport | BOM — 374 movements (205 dep / 169 arr) |
| Flight share by airline | IndiGo 27.1%, Air India 25.4%, SpiceJet 24.6%, Vistara 22.9% |
| Anomalies | **2 (0.2%)** |
| Bookings | 1,000 for 636 distinct passengers |
| Cancellation rate | 33.9% (of bookings with a known status) |
| Total revenue | ₹73.85 lakh across 922 valued payments |

Duration is remarkably even across carriers — 163.8 to 165.4 minutes — which is
itself the finding: no airline runs systematically longer sectors on this network.

### On "Delays"

The brief asks for delay analysis. **This dataset cannot support it, and no delay
metric is fabricated.** A delay is actual departure minus *scheduled* departure, and
there is exactly one departure timestamp per flight with no schedule to compare
against. The measurable substitute — duration anomaly detection — is what was built.
Adding a `scheduled_departure_time` column to the feed would make true delay metrics
available with no change to the pipeline's shape. See `DECISIONS.md` D-011.

---

## Privacy and access control

PII is protected with four techniques, chosen per column by what the analysis
actually needs (`DECISIONS.md` D-015):

| Technique | Columns | Why this one |
|---|---|---|
| Salted SHA-256 (16 hex chars) | `aadhaar_id`, `passport_number`, `passenger_id` | Must still `JOIN` and `COUNT DISTINCT` exactly |
| Partial mask | `email` → `v***********e@gmail.com`, `phone` → `+91-68****3790` | An operator needs to verify a contact |
| Generalisation | `date_of_birth` → `age_band` | An exact DOB re-identifies; a band does not |
| Drop | `first_name`, `last_name`, `emergency_contact_name`, `emergency_contact_phone` | Answer no KPI here |

**Controls in place:**

- `privacy.assert_no_raw_pii()` fails the run outright if a raw PII column ever
  reaches a curated table. It is not advisory — it raises.
- Raw PII exists only in `data/bronze/`, which is gitignored and never published.
- The salt comes from the `ASG_PII_SALT` environment variable and is never
  committed. The documented local default exists only so a reviewer with no `.env`
  can reproduce the run.
- The `.pbix` reads `data/gold/` only, so the dashboard is safe to share as-is.

**Access model for a production deployment:** three roles on the medallion zones —
*Data Engineer* (read/write bronze–gold), *Analyst* (read gold only), *Auditor*
(read quarantine and logs, no PII). On Azure this is Entra ID groups with RBAC and
POSIX ACLs per container; the salt lives in Key Vault, not an environment variable.
In Power BI, row-level security restricts analysts to their own region's routes, and
re-identification requires the salt, which no analyst role holds.

---

## Scalability and performance

Honest position: the transform layer is a single pandas process. At 4,059 rows it
finishes in seconds; it will not survive a hundred million.

What makes the migration cheap is that the *structure* is already the cloud
structure. The bronze/silver/gold zones, the stage separation, and the star schema
are exactly what a Databricks implementation would use. The swap points are narrow:

| Concern | Today | At scale | File to change |
|---|---|---|---|
| Read | `pd.read_excel` | `spark.read` from ADLS Gen2 | `src/ingest.py` |
| Compute | pandas, one process | PySpark on Databricks | `src/clean.py`, `src/transform.py` |
| Write | `to_csv` / `to_parquet` | Delta Lake, partitioned by `departure_date` | `src/model.py` |
| Orchestration | `python -m src.pipeline` | Azure Data Factory scheduled trigger | `src/pipeline.py` unchanged |
| Serving | CSV import | Synapse serverless SQL over gold | Power BI connection |

Business rules stay in `config.py` and would not change at all. Efficiency choices
already made: aggregation before joining (D-016), a single pass over each frame per
stage, no row-wise `apply` on the hot path, and Parquet alongside CSV so a reload
does not re-infer types.

---

## Assumptions

1. The `flight_id` prefix reliably identifies the carrier. Re-proved on every run;
   an unseen prefix stops the pipeline rather than defaulting (D-003).
2. No ASG flight exceeds 24 hours, so a negative interval means the arrival date
   lost a day (D-006).
3. A plausible domestic sector runs 20 minutes to 8 hours; outside that is flagged,
   never deleted.
4. A booking with an unknown status still occupied a seat and still counts as a
   booking (D-012).
5. Where a passenger has conflicting master records, the most complete one is
   correct — there is no timestamp to prefer the most recent (D-014).
6. `payments.booking_id` is a genuine many-to-one relationship: multiple payments
   against one booking are instalments or retries, not duplicates (D-016).
7. The departure window (2026-04-17 to 2026-04-20) is a 4-day operational snapshot,
   so the dashboard reports within-window patterns and no month-over-month trend.

---

## Power BI dashboard

`powerbi/` holds the `.pbix` and screenshots. It loads `data/gold/*.csv` — four
pages, KPI cards, slicers on airline / route / date / time band.

See `docs/powerbi_build_guide.md` for the page-by-page build, the exact source table
behind every visual, and the DAX measures.
