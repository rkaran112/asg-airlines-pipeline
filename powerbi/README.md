# Power BI Report

`asg_airlines_dashboard.pbix` — four pages built on the curated output of the
pipeline. Open it in Power BI Desktop; the data is imported, so it opens without a
connection to any external source.

| Page | Question it answers |
|---|---|
| **Executive Overview** | How is the network performing overall? 1,003 flights, 164.67 min average duration, 1,000 bookings, ₹7.39M valid revenue, 31.4% cancellation rate, 63.7% payment coverage. |
| **Duration & Schedule** | How long do flights take, and when do they run? Includes the 12.2% that cross midnight and the 27.0% red-eye departures, plus a flight-level table showing every corrected record and its reason. |
| **Route & Airline Performance** | Which routes and carriers carry the traffic? 30 routes led by BOM–CCU at 90 flights; near-even four-way airline share. |
| **Data Quality, Pipeline Audit & Governance** | Can the numbers be trusted? All 714 source issue events itemised by table, issue and treatment — the cleaning decisions are auditable from the dashboard itself. |

Every page carries synced slicers for airline, route and booking month.

`screenshots/` holds one PNG per page.

## Note on the delay metric

Page 4 states plainly that operational delay is not computable from this dataset:
the source provides only actual departure and arrival timestamps with no scheduled
times to compare against. Duration anomaly detection is the measurable substitute.
See `DECISIONS.md` D-011.

## Reconciling with the pipeline

The dashboard reports **1,003** flights; `python -m src.pipeline` reports **1,004**.
One record, one deliberate difference:

| | Pipeline (`src/`) | Dashboard model |
|---|---|---|
| Conflicting `flight_id` 6F250 | first kept and flagged, second quarantined | both quarantined |
| Flight count | 1,004 | 1,003 |
| Average duration | 164.8 min | 164.67 min |
| 72 missing/sentinel airlines | repaired from the `flight_id` prefix | retained as an `UNKNOWN` category |

Every other data quality count matches exactly — 15 exact duplicate flights,
41 + 31 airlines, 48 + 30 payment amounts, 45 + 30 booking statuses, 75 duplicate
passenger ids, 10 missing surnames. Both read the same defects from the same source
and differ only in how one conflicting identifier is treated. Full detail is in
section 11.5 of `docs/ASG_Airlines_Documentation.docx`.
