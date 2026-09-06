# Architecture, Data Flow and Data Model

Diagrams render natively on GitHub.

---

## 1. Architecture diagram

```mermaid
flowchart TB
    subgraph SRC["Source systems"]
        XL["UseCase - Airlines.xlsx<br/>flights | bookings<br/>passengers | payments"]
    end

    subgraph BRONZE["BRONZE - raw landing"]
        B["data/bronze/*.csv<br/>verbatim, all columns as text<br/>gitignored - holds raw PII"]
    end

    subgraph SILVER["SILVER - cleaned and protected"]
        S["data/silver/*.csv<br/>typed, de-duplicated, repaired<br/>PII hashed / masked / dropped"]
    end

    subgraph GOLD["GOLD - analytics ready"]
        G1["Star schema<br/>4 dimensions, 2 facts"]
        G2["9 KPI tables"]
    end

    subgraph Q["QUARANTINE"]
        QQ["quarantine.csv<br/>rejected row + rule that rejected it"]
    end

    BI["Power BI Desktop<br/>4 pages, KPI cards, slicers"]

    XL -->|"ingest.py<br/>dtype=str"| B
    B -->|"validate.py - schema contract"| S
    B -->|"clean.py - nulls, dupes, repairs"| S
    B -->|"transform.py - duration, overnight"| S
    B -->|"privacy.py - PII protection"| S
    S -->|"model.py"| G1
    G1 -->|"kpis.py"| G2
    G2 --> BI
    G1 --> BI
    S -.->|"rejected rows"| QQ
    B -.->|"rejected rows"| QQ
```

**Orchestration:** `pipeline.py` is the single entry point (`python -m src.pipeline`).
It owns stage order and the reconciliation report; it contains no business logic.

---

## 2. Data flow diagram

```mermaid
flowchart LR
    A["4,059 source rows"] --> B["[1] INGEST<br/>read as text"]
    B --> C["[2] VALIDATE<br/>schema contract"]
    C --> D["[3] CLEAN"]
    D --> D1["72 airlines repaired<br/>from flight_id prefix"]
    D --> D2["15 exact dupes dropped<br/>1 conflict quarantined"]
    D --> D3["39 passengers collapsed<br/>by completeness"]
    D --> D4["75 statuses to UNKNOWN<br/>78 amounts to null"]
    D1 --> E["[4] TRANSFORM"]
    D2 --> E
    D3 --> E
    D4 --> E
    E --> E1["parse mixed<br/>timestamp formats"]
    E1 --> E2["overnight repair<br/>+1 day if arr &lt; dep"]
    E2 --> E3["duration_minutes<br/>cross-checked 1003/1003"]
    E3 --> E4["anomaly flags<br/>per-route Tukey fence"]
    E4 --> F["[5] PRIVACY<br/>hash | mask | generalise | drop"]
    F --> F1{"assert_no_raw_pii<br/>guard rail"}
    F1 -->|pass| G["[6] MODEL<br/>star schema"]
    F1 -->|fail| X["run stops"]
    G --> G1["payments aggregated<br/>to booking grain FIRST"]
    G1 --> H["[7] KPIS<br/>9 tables"]
    H --> I["4,004 curated rows<br/>1 quarantined | 54 deduplicated"]
```

---

## 3. Data model

```mermaid
erDiagram
    dim_date ||--o{ dim_flight : "departure_date"
    dim_route ||--o{ dim_flight : "route"
    dim_flight ||--o{ fact_booking : "flight_id"
    dim_passenger ||--o{ fact_booking : "passenger_id"
    fact_booking ||--o{ fact_payment : "booking_id"

    dim_flight {
        string flight_id PK
        string airline
        string source
        string destination
        string route FK
        datetime departure_time
        datetime arrival_time
        date departure_date FK
        int departure_hour
        string departure_time_band
        int duration_minutes
        bool is_overnight
        int route_median_duration_minutes
        bool is_duration_outlier
        bool airline_was_repaired
        bool arrival_date_was_repaired
        bool is_anomaly
        string anomaly_reason
    }
    dim_passenger {
        string passenger_id PK
        string passenger_key "hashed"
        string aadhaar_hash "hashed"
        string email_masked "masked"
        string email_domain
        string phone_masked "masked"
        string gender
        int age
        string age_band "generalised"
    }
    dim_route {
        string route PK
        string source
        string destination
        int flights_operated
        float avg_duration_minutes
        int overnight_flights
        int anomalies
    }
    dim_date {
        date date PK
        int year
        int month
        string month_name
        string day_name
        bool is_weekend
    }
    fact_booking {
        string booking_id PK
        string passenger_id FK
        string flight_id FK
        datetime booking_date
        string status
        bool status_was_unknown
        string seat_number
        string passport_hash "hashed"
        float booking_total_amount "pre-aggregated"
        int payment_count
        bool has_payment
    }
    fact_payment {
        string payment_id PK
        string booking_id FK
        float amount
        string payment_method
        bool amount_is_missing
    }
```

### Grains

| Table | Grain | Rows |
|---|---|---|
| `dim_flight` | one flight | 1,004 |
| `dim_passenger` | one passenger | 1,000 |
| `dim_route` | one source-destination pair | 30 |
| `dim_date` | one calendar date | 345 |
| `fact_booking` | one booking | 1,000 |
| `fact_payment` | one payment | 1,000 |

**Critical modelling rule:** `fact_booking` and `fact_payment` are never joined to
each other. 1,000 payments belong to only 637 bookings, so a direct join is
one-to-many and would inflate every count and revenue figure. Booking-level revenue
comes from the pre-aggregated `fact_booking[booking_total_amount]`. See
`DECISIONS.md` D-016.
