# Decision Log

Every judgement call made while building this pipeline, why it was made, and what
it costs. Written as the decisions were taken, not reconstructed afterwards.

---

## D-001 — Run the pipeline locally in Python rather than on Azure

**Context:** The brief prefers Azure (ADF / Synapse / Databricks) but allows a local
implementation. An Azure trial subscription was available.

**Options considered:**
1. Databricks or a Synapse Spark pool — the natural fit for this shape of work.
2. ADLS Gen2 + Data Factory + Synapse *serverless* SQL — all serverless, no clusters.
3. Local Python, with the medallion layout Azure would have used.

**Decision:** Option 3.

**Why:** Azure trial subscriptions ship with a 0 vCPU quota, so option 1 cannot
provision a cluster at all. Option 2 is genuinely possible, but wiring storage,
a Data Factory pipeline, external tables and a Power BI gateway is several hours of
work that produces no better *analytics* than running the same transformations
locally. Given the submission deadline, a complete and correct local pipeline beats
a half-configured cloud one.

**Consequence / risk:** The transform layer is a single Python process and will not
scale past what one machine can hold. This is mitigated structurally, not
pretended away: the bronze/silver/gold zone layout, the stage separation, and the
star schema are all exactly what a Databricks implementation would use, so the
migration is a swap of `ingest.py` and the `to_csv`/`to_parquet` calls in
`model.py` for Spark equivalents. See the Scalability section of the README.

---

## D-002 — Read every source column as text, then type it deliberately

**Context:** `pd.read_excel` guesses column types. The brief says the data contains
corrupted values.

**Decision:** `ingest.load_workbook` reads with `dtype=str`. Typing happens later,
inside `clean.py` and `transform.py`.

**Why:** If pandas types the columns at read time, a corrupt value is coerced to
NaN before any code has had a chance to record *that it was corrupt and why*. The
`payments.amount` column is the clear example: 30 cells contain the literal string
`"INVALID"`. Read as text, we can count them, log them, and route them; read as
float, they are indistinguishable from the 48 cells that are genuinely blank.

**Consequence / risk:** Every module must convert explicitly, which is more code.
That is the intended trade — the conversions are the audit trail.

**Where implemented:** `src/ingest.py::load_workbook`

---

## D-003 — Recover the 72 missing airline values from the flight_id prefix

**Context:** 41 `airline` values are null and 31 are the literal string `"UNKNOWN"`
— 72 of 1020 flight rows, 7% of the fleet.

**Options considered:**
1. Drop the rows — loses 7% of flights and skews the airline-distribution KPI.
2. Keep "Unknown" as its own airline — keeps volume, but pollutes every airline
   chart with a fifth carrier that does not exist.
3. Derive the airline from the two-letter `flight_id` prefix.

**Decision:** Option 3. `AI` → Air India, `UK` → Vistara, `SJ` → SpiceJet,
`6F` → IndiGo.

**Why:** The mapping is 1:1 and conflict-free across all 1020 source rows — every
`AI` flight with a populated airline says "Air India", and no prefix ever maps to
two carriers. That makes this a lossless recovery, not an imputation or a guess.

**Consequence / risk:** The mapping is an assumption about ASG's numbering scheme.
If a new carrier is onboarded with an unseen prefix, or a prefix is reused, the
repair would silently mislabel flights. Mitigated by re-proving the mapping against
the data on every run and raising rather than defaulting: an unknown prefix stops
the pipeline, and a prefix that maps to two airlines stops it too.

**Where implemented:** `src/clean.py::repair_airline_from_flight_id`

---

## D-004 — Drop exact duplicate flights, but quarantine conflicting ones

**Context:** 1020 flight rows contain only 1004 distinct `flight_id` values.

**Decision:** 15 rows are byte-identical duplicates and are dropped outright. The
remaining conflict is `6F250`, which appears twice on *different routes with
different durations*. The first occurrence is kept and flagged with
`has_conflicting_source_record`; the second is quarantined.

**Why:** Identical rows carry no information and dropping them is safe. A genuine
conflict is different — we cannot tell from this data which of the two `6F250`
records is the real flight. Keeping one preserves referential integrity for any
booking that references `6F250`, and the flag lets an analyst exclude it. Dropping
both would orphan those bookings; keeping both would fan out every join.

**Consequence / risk:** "First occurrence wins" is arbitrary. It is recorded rather
than hidden — the losing row is in `data/quarantine/quarantine.csv` with its reason,
and the surviving row is flagged in `dim_flight`.

**Where implemented:** `src/clean.py::resolve_conflicting_flight_ids`

---

## D-005 — Parse timestamps with `format="mixed"`

**Context:** The first version of the pipeline quarantined 31 flights for having an
unparsable `arrival_time`. Investigation showed the timestamps were not corrupt at
all — the source mixes two ISO layouts. 1016 departures and 989 arrivals carry
microseconds (`2026-04-20 23:38:41.701000`); 4 departures and 31 arrivals do not
(`2026-04-20 23:20:42`).

**Decision:** Parse with `pd.to_datetime(..., format="mixed")`.

**Why:** pandas infers a single format from the first non-null value and applies it
to the whole column. With `errors="coerce"`, every minority-format row silently
becomes `NaT` — so a bug in the parsing step was masquerading as a data quality
problem and was about to throw away 31 perfectly valid flights. This is precisely
the "inconsistent time formats" defect the brief names, and the fix is to handle it,
not to quarantine it.

**Consequence / risk:** `format="mixed"` is slower than a fixed format because each
value is parsed individually. At 1020 rows this is irrelevant; at 100 million rows
the right move is to normalise the format at ingestion instead.

**Where implemented:** `src/transform.py::parse_timestamps`

---

## D-006 — Handle overnight flights by repairing the arrival date, not by negating

**Context:** Flights that depart at 23:38 and land at 02:32 the next morning must
produce a positive duration, and the brief calls this out specifically.

**Decision:** If `arrival_time < departure_time`, add one day to the arrival, then
subtract. `is_overnight` is then defined as landing on a later calendar date than
departure.

**Why:** The source timestamps already carry full dates, so the 122 genuine
overnight flights subtract correctly with no intervention — they never enter the
repair branch. The +1 day rule is the defensive path for the one row where the
arrival *date* itself is wrong, and for any future feed that supplies clock times
without dates.

**Consequence / risk:** The rule assumes no ASG flight exceeds 24 hours, which is
safe for a domestic Indian network but would need revisiting for long-haul.

**Where implemented:** `src/transform.py::compute_duration`

---

## D-007 — Repair SJ192 rather than quarantine it

**Context:** `SJ192` departs `2026-04-19 18:45` and records arrival at
`2026-04-18 23:45` — arrival is 19 hours *before* departure. This looked like an
irreparably corrupt record.

**Decision:** Treat it as a lost-a-day arrival date and apply the D-006 repair,
giving a duration of 300 minutes.

**Why:** The repaired value is independently confirmed. The source workbook's own
`duration` column — an Excel formula, `=F356-E356` — evaluates to `05:00:00` for
this row, exactly 300 minutes. Two independent derivations agreeing is much stronger
evidence than the arrival timestamp alone, and 300 minutes also sits inside the
observed fleet duration range (30–300 minutes). Deleting a recoverable record would
have been the worse error.

**Consequence / risk:** The flight is still flagged with
`arrival_date_was_repaired = True` and appears on the anomaly page, so the repair is
visible to anyone reading the dashboard rather than buried in the pipeline.

**Where implemented:** `src/transform.py::compute_duration`, verified by
`src/transform.py::crosscheck_against_source_duration`

---

## D-008 — Never trust the source `duration` column, but use it once as a check

**Context:** The `flights.duration` column is a live Excel formula (`=F2-E2`), not
recorded data.

**Decision:** The column is renamed `_source_duration` at the cleaning stage, used
in exactly one place to cross-check our own recomputed duration, and then dropped
before the data reaches the star schema.

**Why:** A formula result is only as good as the cells it points at, and it cannot
see any repair we make — so it must never reach a KPI. But as a second opinion
computed by a completely different tool, it is genuinely valuable: the run log
reports **1003 of 1003 comparable rows agree**, which is real evidence the duration
logic is right rather than merely self-consistent.

**Where implemented:** `src/transform.py::crosscheck_against_source_duration`

---

## D-009 — Detect duration outliers per route with a Tukey fence, not a fixed tolerance

**Context:** The anomaly rule started as "flag any flight more than 90 minutes from
its route median". It flagged 298 flights — 30% of the fleet.

**Decision:** Replaced with the standard Tukey fence applied *per route*: a flight
is an outlier if its duration falls more than 1.5 × IQR outside that route's own
quartiles.

**Why:** Profiling showed durations on every single route span roughly 30–293
minutes with a standard deviation near 75 minutes. There is no tight per-route
duration to deviate *from*, so a fixed 90-minute band was just measuring the natural
spread and calling a third of the fleet anomalous. An anomaly report that flags 30%
of records is one nobody reads. The IQR fence adapts to each route's own spread and
now flags 2 flights (0.2%) — the conflicting `6F250` and the repaired `SJ192`.

**Consequence / risk:** With this data being as evenly spread as it is, few
duration outliers will ever be found. That is the honest finding and it is stated
on the dashboard rather than tuned around.

**Where implemented:** `src/transform.py::flag_anomalies`, threshold in
`src/config.py`

---

## D-010 — A repaired airline is not an anomaly

**Context:** The first anomaly rule included `airline_was_repaired`, putting all 68
repaired flights on the anomaly page.

**Decision:** Removed from `is_anomaly`. The flag is kept as its own column.

**Why:** D-003 established the repair is lossless and verified. A record we have
*fixed with certainty* is trustworthy; flagging it as anomalous conflates "this data
was dirty" with "this flight is suspicious", and buries the two real anomalies under
68 non-events. Operations wants the second question answered, and data governance
wants the first — so they get separate columns.

**Where implemented:** `src/transform.py::flag_anomalies`

---

## D-011 — Do not invent a delay metric

**Context:** The brief asks for "Delays / Anomalies".

**Decision:** No delay KPI is produced. The dashboard has an Anomaly page instead,
and the documentation states plainly why.

**Why:** A delay is actual departure minus *scheduled* departure. This dataset has
one departure timestamp per flight and no schedule to compare it against. Any
"delay" computed from these columns — against the route mean, against a made-up
on-time target — would be a fabricated number presented as an operational fact,
which is worse than an honest gap. The measurable, real substitute is duration
anomaly detection, which is what was built.

**Consequence / risk:** Ingesting a schedule feed with `scheduled_departure_time`
would make true delay metrics available with no change to the pipeline shape — the
column simply joins onto `dim_flight`.

---

## D-012 — Label unknown booking statuses instead of dropping the bookings

**Context:** 45 `status` values are null and 30 are the literal string `"INVALID"`.

**Decision:** All 75 become `UNKNOWN`, flagged with `status_was_unknown`, and the
bookings stay in the fact table. The cancellation-rate KPI divides only by bookings
whose status is actually known.

**Why:** The status is unknown, but the booking is a real event — a passenger, a
seat and usually a payment. Dropping it would understate booking volume and revenue.
Keeping it while counting it in the cancellation denominator would drag that rate
down by 7.5% for no reason, so the denominator excludes it explicitly.

**Where implemented:** `src/clean.py::clean_bookings`, `src/kpis.py::kpi_headline`

---

## D-013 — Missing payment amounts stay null; they are never zero-filled

**Context:** 78 of 1000 payment amounts are unusable — 48 blank, 30 the string
`"INVALID"`.

**Decision:** Both become `NaN`, flagged with `amount_is_missing`, and are excluded
from every revenue aggregate rather than filled with 0.

**Why:** A zero is a *statement* that a payment of nothing was made. It sums into
revenue, drags down the average payment, and makes a data gap look like a business
fact. Null propagates correctly through `sum()` and `mean()` and keeps the gap
visible and countable.

**Consequence / risk:** Total revenue understates the true figure by whatever those
78 payments were worth. This is stated on the dashboard rather than papered over.

**Where implemented:** `src/clean.py::clean_payments`

---

## D-014 — Collapse duplicate passengers by completeness

**Context:** 1039 passenger rows contain only 1000 distinct `passenger_id` values.
The 39 duplicates have *differing* attributes, so they are conflicting master
records, not copies.

**Decision:** Keep the row with the fewest missing fields; break ties by first
appearance.

**Why:** A passenger dimension is only useful if populated, so completeness is the
most defensible survivorship rule available. The obvious alternative — keep the most
recent — is impossible: there is no `updated_at` or load-timestamp column.

**Consequence / risk:** If the *less* complete record is the more recent and correct
one, we keep stale attributes. Since every attribute we keep is either hashed or
generalised into a band (D-015), the analytical impact is negligible.

**Where implemented:** `src/clean.py::resolve_duplicate_passengers`

---

## D-015 — Four different PII techniques, chosen per column by what analytics needs

**Context:** The dataset carries real categories of PII: names, email, phone,
Aadhaar number, date of birth, passport number, and emergency contact details.
Masking everything identically would break the analysis; masking nothing is not an
option.

**Decision:**

| Technique | Columns | Reason |
|---|---|---|
| Salted SHA-256, truncated to 16 hex chars | `aadhaar_id`, `passport_number`, `passenger_id` | Must still `JOIN` and `COUNT DISTINCT` exactly. Deterministic, so counts stay correct; unreversible without the salt. |
| Format-preserving partial mask | `email`, `phone` | An operator sometimes needs to eyeball or verify a contact. `r****h@gmail.com` keeps the domain for provider analysis. |
| Generalisation | `date_of_birth` → `age_band` | An exact DOB is a strong re-identification key; a band answers every demographic question in this brief. |
| Drop entirely | `first_name`, `last_name`, `emergency_contact_name`, `emergency_contact_phone` | They answer no KPI here. The safest handling of data you do not need is not to carry it. |

**Why:** Blanket hashing would have destroyed the ability to analyse email domains
and age distribution; blanket masking would have broken passenger counts. Matching
the technique to the analytical need is the whole point.

**Consequence / risk:** The hash is only as strong as the salt. The salt is read
from `ASG_PII_SALT` and never committed; the documented local default exists so a
reviewer with no `.env` can still reproduce the run, and is not a production
secret. A `privacy.assert_no_raw_pii` guard rail fails the run outright if any raw
PII column ever reaches a curated table.

**Where implemented:** `src/privacy.py`

---

## D-016 — Keep payments at their own grain instead of joining them onto bookings

**Context:** 1000 payment rows belong to only 637 distinct bookings — 363 bookings
have more than one payment.

**Decision:** `fact_payment` stays at payment grain. `fact_booking` carries a
pre-aggregated `booking_total_amount`, `payment_count` and
`payments_missing_amount`. The two facts are never joined to each other; both join
to the shared dimensions.

**Why:** `bookings JOIN payments` is a one-to-many join, so it duplicates booking
rows. Every count, every average and every revenue figure downstream would silently
inflate — the single easiest way to get this case study numerically wrong, and it
produces no error message. Aggregating to booking grain first makes the join 1:1.

**Consequence / risk:** Analysts must remember not to relate the two fact tables in
Power BI. `model.build_fact_booking` asserts the row count is unchanged after the
join, so if anyone reintroduces the fan-out the pipeline fails instead of producing
quietly wrong numbers.

**Where implemented:** `src/model.py::summarise_payments_by_booking`,
`src/model.py::build_fact_booking`

---

## D-017 — Quarantine rejected rows instead of dropping them

**Context:** Several rules reject records. A pipeline that silently drops rows
cannot be audited.

**Decision:** Every rejection goes through `validate.reject`, which writes the row,
its source sheet and the rule that rejected it to
`data/quarantine/quarantine.csv`. The run ends with a reconciliation report.

**Why:** `rows_in = rows_out + quarantined + deduplicated` must hold, and a reviewer
must be able to see every row that did not make it and why. On the final run exactly
one row is quarantined (the conflicting `6F250`), so all 1020 flights, 1000
bookings, 1039 passengers and 1000 payments are accounted for.

**Where implemented:** `src/validate.py`, `src/pipeline.py::report_reconciliation`
