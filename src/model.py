"""Stage 6 - build the star schema that Power BI connects to.

Owns:   the shape of the gold zone.
Called by: pipeline.run()
Never:  cleans or derives measures - it only reshapes what earlier stages produced.

    dim_flight     one row per flight_id      (grain: a flight)
    dim_passenger  one row per passenger_id   (grain: a passenger, PII-safe)
    dim_route      one row per source-dest    (grain: a route)
    dim_date       one row per calendar date  (grain: a day)
    fact_booking   one row per booking_id     (grain: a booking)
    fact_payment   one row per payment_id     (grain: a payment)

The payment fan-out trap: 1000 payments belong to only 637 bookings, so joining
payments straight onto bookings would duplicate booking rows and inflate every
count and revenue figure. fact_payment therefore stays at its own grain, and
fact_booking carries a pre-aggregated booking_total_amount instead. Power BI joins
each fact to the dimensions, never one fact to the other.
"""

import logging

import pandas as pd

from src import config

log = logging.getLogger(__name__)


def build_dim_flight(flights):
    columns = ["flight_id", "airline", "source", "destination", "route",
               "departure_time", "arrival_time", "departure_date", "departure_hour",
               "departure_time_band", "duration_minutes", "is_overnight",
               "route_median_duration_minutes", "duration_vs_route_median_minutes",
               "is_duration_outlier", "is_implausible_duration",
               "airline_was_repaired", "arrival_date_was_repaired",
               "has_conflicting_source_record", "is_anomaly", "anomaly_reason"]
    return flights[columns].copy()


def build_dim_route(flights):
    """Route-level reference table, including how busy and how long each route is."""
    dim = (flights.groupby(["route", "source", "destination"], as_index=False)
           .agg(flights_operated=("flight_id", "count"),
                avg_duration_minutes=("duration_minutes", "mean"),
                median_duration_minutes=("duration_minutes", "median"),
                overnight_flights=("is_overnight", "sum"),
                anomalies=("is_anomaly", "sum")))
    dim["avg_duration_minutes"] = dim["avg_duration_minutes"].round(1)
    return dim


def build_dim_date(flights, bookings):
    """One row per date touched by either fact, so both can share a date slicer."""
    dates = pd.concat([
        pd.to_datetime(flights["departure_time"]).dt.normalize(),
        pd.to_datetime(bookings["booking_date"]).dt.normalize(),
    ]).dropna().drop_duplicates().sort_values()

    dim = pd.DataFrame({"date": dates.reset_index(drop=True)})
    dim["year"] = dim["date"].dt.year
    dim["month"] = dim["date"].dt.month
    dim["month_name"] = dim["date"].dt.month_name()
    dim["day"] = dim["date"].dt.day
    dim["day_name"] = dim["date"].dt.day_name()
    dim["is_weekend"] = dim["date"].dt.dayofweek >= 5
    return dim


def summarise_payments_by_booking(payments):
    """Collapse payments to one row per booking - the fix for the fan-out trap.

    Null amounts are excluded from the sum rather than counted as zero, so a
    booking whose only payment row is missing an amount gets a null total, not a
    misleading 0.00.
    """
    # min_count=1 matters: a plain sum() returns 0.0 for a group whose amounts are
    # all null, which would report a data gap as revenue of zero - exactly what
    # DECISIONS.md D-013 forbids. With min_count=1 an all-null group stays null.
    summary = (payments.groupby("booking_id", as_index=False)
               .agg(booking_total_amount=("amount", lambda s: s.sum(min_count=1)),
                    payment_count=("payment_id", "count"),
                    payments_missing_amount=("amount_is_missing", "sum")))
    summary["booking_total_amount"] = summary["booking_total_amount"].round(2)
    log.info("Collapsed %d payment rows to %d booking-level totals",
             len(payments), len(summary))
    return summary


def build_fact_booking(bookings, payment_summary):
    fact = bookings.merge(payment_summary, on="booking_id", how="left")
    fact["has_payment"] = fact["payment_count"].notna()
    fact["payment_count"] = fact["payment_count"].fillna(0).astype(int)

    if len(fact) != len(bookings):
        raise ValueError("fact_booking fanned out - the payment join is not 1:1")

    return fact


def build_star_schema(flights, bookings, passengers, payments):
    """Assemble every gold table and return them keyed by table name."""
    payment_summary = summarise_payments_by_booking(payments)

    tables = {
        "dim_flight": build_dim_flight(flights),
        "dim_passenger": passengers,
        "dim_route": build_dim_route(flights),
        "dim_date": build_dim_date(flights, bookings),
        "fact_booking": build_fact_booking(bookings, payment_summary),
        "fact_payment": payments,
    }

    for name, table in tables.items():
        log.info("  %-14s %5d rows x %2d columns", name, len(table), table.shape[1])
    return tables


def write_gold(tables):
    """Write every table as both CSV (Power BI import) and Parquet (typed reload)."""
    config.GOLD_DIR.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        table.to_csv(config.GOLD_DIR / f"{name}.csv", index=False)
        try:
            table.to_parquet(config.GOLD_DIR / f"{name}.parquet", index=False)
        except (ImportError, ValueError) as exc:
            log.warning("Parquet skipped for %s (%s); CSV written", name, exc)
    log.info("Gold written: %s", config.GOLD_DIR)
