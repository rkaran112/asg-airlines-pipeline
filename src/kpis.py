"""Stage 7 - the business metrics, pre-aggregated for the dashboard.

Owns:   every number the reviewer will read off the Power BI pages.
Called by: pipeline.run()
Never:  recomputes a derived column - it only groups what model.py produced.

Every KPI table below states its grain in its docstring, because a metric without
a grain is not a metric.
"""

import logging

import pandas as pd

from src import config

log = logging.getLogger(__name__)


def kpi_headline(flights, bookings, payments):
    """Grain: one row. The KPI cards across the top of every dashboard page."""
    confirmed = bookings["status"] == "CONFIRMED"
    cancelled = bookings["status"] == "CANCELLED"
    known_status = bookings["status"] != config.UNKNOWN_STATUS

    return pd.DataFrame([{
        "total_flights": len(flights),
        "total_bookings": len(bookings),
        "total_passengers": bookings["passenger_id"].nunique(),
        "airlines_operating": flights["airline"].nunique(),
        "routes_operated": flights["route"].nunique(),
        "avg_flight_duration_minutes": round(flights["duration_minutes"].mean(), 1),
        "median_flight_duration_minutes": float(flights["duration_minutes"].median()),
        "overnight_flights": int(flights["is_overnight"].sum()),
        "overnight_flight_pct": round(100 * flights["is_overnight"].mean(), 1),
        "anomaly_flights": int(flights["is_anomaly"].sum()),
        "anomaly_pct": round(100 * flights["is_anomaly"].mean(), 1),
        "confirmed_bookings": int(confirmed.sum()),
        # Cancellation rate uses only bookings whose status is actually known, so
        # the 75 unknown-status rows cannot dilute it.
        "cancellation_rate_pct": round(
            100 * cancelled.sum() / max(known_status.sum(), 1), 1),
        "total_revenue": round(payments["amount"].sum(), 2),
        "avg_payment_amount": round(payments["amount"].mean(), 2),
    }])


def kpi_duration_by_airline(flights):
    """Grain: one row per airline. Feeds the Duration Analysis page."""
    out = (flights.groupby("airline", as_index=False)
           .agg(flights_operated=("flight_id", "count"),
                avg_duration_minutes=("duration_minutes", "mean"),
                median_duration_minutes=("duration_minutes", "median"),
                min_duration_minutes=("duration_minutes", "min"),
                max_duration_minutes=("duration_minutes", "max"),
                overnight_flights=("is_overnight", "sum"),
                anomalies=("is_anomaly", "sum")))
    out["avg_duration_minutes"] = out["avg_duration_minutes"].round(1)
    out["share_of_flights_pct"] = (100 * out["flights_operated"]
                                   / out["flights_operated"].sum()).round(1)
    return out.sort_values("flights_operated", ascending=False)


def kpi_route_traffic(flights, bookings):
    """Grain: one row per route. Feeds the Route Performance page."""
    traffic = (flights.groupby(["route", "source", "destination"], as_index=False)
               .agg(flights_operated=("flight_id", "count"),
                    avg_duration_minutes=("duration_minutes", "mean"),
                    overnight_flights=("is_overnight", "sum"),
                    anomalies=("is_anomaly", "sum")))

    bookings_per_route = (bookings.merge(flights[["flight_id", "route"]], on="flight_id")
                          .groupby("route", as_index=False)
                          .agg(bookings=("booking_id", "count"),
                               revenue=("booking_total_amount", "sum")))

    out = traffic.merge(bookings_per_route, on="route", how="left")
    out["avg_duration_minutes"] = out["avg_duration_minutes"].round(1)
    out["revenue"] = out["revenue"].round(2)
    out["bookings"] = out["bookings"].fillna(0).astype(int)
    return out.sort_values("flights_operated", ascending=False)


def kpi_airport_traffic(flights):
    """Grain: one row per airport. Departures and arrivals, for the traffic map."""
    departures = flights.groupby("source").size().rename("departures")
    arrivals = flights.groupby("destination").size().rename("arrivals")
    out = pd.concat([departures, arrivals], axis=1).fillna(0).astype(int)
    out["total_movements"] = out["departures"] + out["arrivals"]
    return out.reset_index(names="airport").sort_values("total_movements", ascending=False)


def kpi_hourly_load(flights):
    """Grain: one row per hour of day. Shows when the network is busiest."""
    out = (flights.groupby("departure_hour", as_index=False)
           .agg(flights_departing=("flight_id", "count"),
                avg_duration_minutes=("duration_minutes", "mean"),
                overnight_flights=("is_overnight", "sum")))
    out["avg_duration_minutes"] = out["avg_duration_minutes"].round(1)
    return out


def kpi_anomalies(flights):
    """Grain: one row per anomalous flight. The Delay / Anomaly Insights detail table."""
    columns = ["flight_id", "airline", "route", "departure_time", "arrival_time",
               "duration_minutes", "route_median_duration_minutes",
               "duration_vs_route_median_minutes", "is_overnight", "anomaly_reason"]
    return (flights.loc[flights["is_anomaly"], columns]
            .sort_values("anomaly_reason").reset_index(drop=True))


def kpi_booking_status(bookings):
    """Grain: one row per booking status. Booking health mix."""
    out = bookings.groupby("status", as_index=False).agg(
        bookings=("booking_id", "count"),
        revenue=("booking_total_amount", "sum"))
    out["revenue"] = out["revenue"].round(2)
    out["share_pct"] = (100 * out["bookings"] / out["bookings"].sum()).round(1)
    return out.sort_values("bookings", ascending=False)


def kpi_payment_method(payments):
    """Grain: one row per payment method. Revenue mix by channel."""
    out = payments.groupby("payment_method", as_index=False).agg(
        payments=("payment_id", "count"),
        revenue=("amount", "sum"),
        avg_amount=("amount", "mean"))
    out["revenue"] = out["revenue"].round(2)
    out["avg_amount"] = out["avg_amount"].round(2)
    return out.sort_values("revenue", ascending=False)


def kpi_passenger_demographics(passengers):
    """Grain: one row per age band and gender. Computed on masked data only."""
    return (passengers.groupby(["age_band", "gender"], as_index=False)
            .agg(passengers=("passenger_id", "count")))


def build_all(tables):
    """Run every KPI and return them keyed by output table name."""
    flights = tables["dim_flight"]
    bookings = tables["fact_booking"]
    payments = tables["fact_payment"]
    passengers = tables["dim_passenger"]

    return {
        "kpi_headline": kpi_headline(flights, bookings, payments),
        "kpi_duration_by_airline": kpi_duration_by_airline(flights),
        "kpi_route_traffic": kpi_route_traffic(flights, bookings),
        "kpi_airport_traffic": kpi_airport_traffic(flights),
        "kpi_hourly_load": kpi_hourly_load(flights),
        "kpi_anomalies": kpi_anomalies(flights),
        "kpi_booking_status": kpi_booking_status(bookings),
        "kpi_payment_method": kpi_payment_method(payments),
        "kpi_passenger_demographics": kpi_passenger_demographics(passengers),
    }


def write_all(kpi_tables):
    config.GOLD_DIR.mkdir(parents=True, exist_ok=True)
    for name, table in kpi_tables.items():
        table.to_csv(config.GOLD_DIR / f"{name}.csv", index=False)
        log.info("  %-28s %4d rows", name, len(table))
    log.info("KPI tables written: %s", config.GOLD_DIR)
