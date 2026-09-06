"""Stage 3 - repair what can be repaired, standardise the rest.

Owns:   null tokens, duplicates, casing/whitespace, and the airline repair.
Called by: pipeline.run()
Never:  parses timestamps or computes durations - that is transform.py.

The ordering inside clean_flights matters and is deliberate:
normalise -> drop exact duplicates -> resolve id conflicts -> repair airline.
Repairing before de-duplicating would repair the same row twice and make the
"72 rows repaired" number in the run report wrong.
"""

import logging

import numpy as np
import pandas as pd

from src import config, validate

log = logging.getLogger(__name__)


def _normalise_null_tokens(df, columns):
    """Turn 'UNKNOWN', 'N/A', '' and friends into real NaN so pandas can see them."""
    for col in columns:
        stripped = df[col].astype(str).str.strip()
        df[col] = stripped.where(~stripped.str.upper().isin(config.NULL_TOKENS), np.nan)
    return df


# --- flights -------------------------------------------------------------

def repair_airline_from_flight_id(flights):
    """Recover the 72 missing/UNKNOWN airline values from the flight_id prefix.

    The two-letter prefix identifies the carrier. This is only a lossless recovery
    if the mapping is 1:1, so the mapping is re-proved against the data on every
    run before it is trusted. If ASG ever adds a carrier, or reuses a prefix, this
    raises rather than quietly mislabelling flights.
    """
    prefix = flights["flight_id"].str[:2]

    unknown_prefixes = sorted(set(prefix.dropna()) - set(config.PREFIX_TO_AIRLINE))
    if unknown_prefixes:
        raise ValueError(
            f"flight_id prefix(es) {unknown_prefixes} are not in PREFIX_TO_AIRLINE. "
            "Add them to config before running - do not guess the carrier.")

    # Prove 1:1: for every prefix, the non-null airlines present must be exactly one.
    conflicts = (flights.assign(_prefix=prefix)
                 .dropna(subset=["airline"])
                 .groupby("_prefix")["airline"].nunique())
    if (conflicts > 1).any():
        bad = conflicts[conflicts > 1].to_dict()
        raise ValueError(f"flight_id prefix maps to multiple airlines: {bad}")

    repaired = flights["airline"].isna()
    flights["airline"] = flights["airline"].fillna(prefix.map(config.PREFIX_TO_AIRLINE))
    flights["airline_was_repaired"] = repaired

    log.info("Repaired airline for %d flight(s) from the flight_id prefix", int(repaired.sum()))
    return flights


def resolve_conflicting_flight_ids(flights):
    """Keep one row per flight_id; quarantine genuine conflicts.

    Exact duplicate rows are dropped first - they carry no information. What is
    left is a flight_id that appears twice with *different* attributes, which is a
    real data conflict we cannot resolve from this data alone. We keep the first
    occurrence so bookings referencing that id still join, flag it, and quarantine
    the losing row so the conflict is visible rather than lost.
    """
    before = len(flights)
    flights = flights.drop_duplicates()
    log.info("Dropped %d exact duplicate flight row(s)", before - len(flights))

    conflicting_ids = flights.loc[flights["flight_id"].duplicated(), "flight_id"].unique()
    flights["has_conflicting_source_record"] = flights["flight_id"].isin(conflicting_ids)

    losers = flights["flight_id"].duplicated(keep="first")
    flights = validate.reject(
        flights, losers,
        "duplicate flight_id with conflicting attributes; first occurrence kept",
        "flights")

    return flights


def clean_flights(flights):
    flights = _normalise_null_tokens(
        flights, ["flight_id", "airline", "source", "destination"])

    flights["flight_id"] = flights["flight_id"].str.upper()
    for col in ["source", "destination"]:
        flights[col] = flights[col].str.upper()

    flights = validate.reject(
        flights, flights["flight_id"].isna(), "flight_id is missing", "flights")
    flights = validate.reject(
        flights, ~flights["source"].isin(config.VALID_AIRPORTS),
        "source is not a recognised ASG airport", "flights")
    flights = validate.reject(
        flights, ~flights["destination"].isin(config.VALID_AIRPORTS),
        "destination is not a recognised ASG airport", "flights")
    flights = validate.reject(
        flights, flights["source"] == flights["destination"],
        "source and destination are the same airport", "flights")

    flights = resolve_conflicting_flight_ids(flights)
    flights = repair_airline_from_flight_id(flights)

    # The source 'duration' column is a live Excel formula (=F2-E2), not recorded
    # data, so it must never reach a KPI. It is renamed rather than dropped so that
    # transform.py can use it once, as an independent cross-check on our own
    # recomputed duration, before discarding it.
    flights = flights.rename(columns={"duration": "_source_duration"})

    return flights.reset_index(drop=True)


# --- passengers ----------------------------------------------------------

def resolve_duplicate_passengers(passengers):
    """Collapse 39 conflicting passenger_id records to one master row each.

    Survivorship rule: keep the row with the fewest missing fields, breaking ties
    by first appearance. Rationale is completeness - a passenger dimension is only
    useful if its attributes are populated, and we have no updated_at column that
    would let us prefer the most recent record instead.
    """
    duplicated_ids = passengers.loc[passengers["passenger_id"].duplicated(), "passenger_id"]
    if duplicated_ids.empty:
        return passengers

    passengers = passengers.copy()
    passengers["_completeness"] = passengers.notna().sum(axis=1)
    passengers["_order"] = range(len(passengers))

    winners = (passengers.sort_values(["_completeness", "_order"], ascending=[False, True])
               .drop_duplicates(subset="passenger_id", keep="first")
               .sort_values("_order"))

    log.info("Collapsed %d duplicate passenger record(s) to %d master row(s)",
             len(passengers) - len(winners), passengers["passenger_id"].nunique())

    return winners.drop(columns=["_completeness", "_order"]).reset_index(drop=True)


def clean_passengers(passengers):
    passengers = _normalise_null_tokens(
        passengers, ["passenger_id", "first_name", "last_name", "gender", "email", "phone"])

    passengers["passenger_id"] = passengers["passenger_id"].str.upper()
    passengers["gender"] = passengers["gender"].str.upper().str[:1]
    passengers["age"] = pd.to_numeric(passengers["age"], errors="coerce")
    passengers["date_of_birth"] = pd.to_datetime(passengers["date_of_birth"], errors="coerce")

    passengers = validate.reject(
        passengers, passengers["passenger_id"].isna(), "passenger_id is missing", "passengers")
    passengers = validate.reject(
        passengers, ~passengers["age"].between(0, 120), "age is outside 0-120", "passengers")

    # 10 rows have no last_name. The name is hashed away in privacy.py anyway, so a
    # blank is harmless here and dropping the passenger would lose their bookings.
    passengers["last_name"] = passengers["last_name"].fillna("")

    return resolve_duplicate_passengers(passengers)


# --- bookings ------------------------------------------------------------

def clean_bookings(bookings):
    """Standardise status; unknown statuses are labelled, not deleted.

    45 nulls and 30 'INVALID' become UNKNOWN. The booking itself is still a real
    event that occupied a seat, so it stays in the fact table. kpis.py excludes
    UNKNOWN from the cancellation-rate denominator so the rate is not diluted.
    """
    bookings = _normalise_null_tokens(
        bookings, ["booking_id", "passenger_id", "flight_id", "status", "seat_number"])

    for col in ["booking_id", "passenger_id", "flight_id"]:
        bookings[col] = bookings[col].str.upper()

    bookings["status"] = (bookings["status"].str.upper()
                          .where(lambda s: s.isin(config.VALID_BOOKING_STATUSES)))
    unknown = bookings["status"].isna()
    bookings["status"] = bookings["status"].fillna(config.UNKNOWN_STATUS)
    bookings["status_was_unknown"] = unknown
    log.info("Labelled %d booking(s) with a missing or invalid status as UNKNOWN",
             int(unknown.sum()))

    bookings["booking_date"] = pd.to_datetime(bookings["booking_date"], errors="coerce")
    bookings["seat_number"] = bookings["seat_number"].str.upper()

    bookings = validate.reject(
        bookings, bookings["booking_id"].isna(), "booking_id is missing", "bookings")
    bookings = validate.reject(
        bookings, bookings["booking_id"].duplicated(), "duplicate booking_id", "bookings")
    bookings = validate.reject(
        bookings, bookings["booking_date"].isna(), "booking_date could not be parsed", "bookings")

    return bookings.reset_index(drop=True)


# --- payments ------------------------------------------------------------

def clean_payments(payments):
    """Type the amount and standardise the method. Null amounts are kept as null.

    48 amounts are missing. They are deliberately NOT zero-filled: a zero would be
    summed into revenue as a real payment of nothing, understating averages and
    hiding the gap. Null propagates correctly through sum() and mean() instead.
    """
    payments = _normalise_null_tokens(
        payments, ["payment_id", "booking_id", "payment_method"])

    payments["payment_id"] = payments["payment_id"].str.upper()
    payments["booking_id"] = payments["booking_id"].str.upper()
    payments["payment_method"] = payments["payment_method"].str.upper()
    payments["amount"] = pd.to_numeric(payments["amount"], errors="coerce")
    payments["amount_is_missing"] = payments["amount"].isna()

    payments = validate.reject(
        payments, payments["payment_id"].isna(), "payment_id is missing", "payments")
    payments = validate.reject(
        payments, payments["payment_id"].duplicated(), "duplicate payment_id", "payments")
    payments = validate.reject(
        payments, payments["amount"] < 0, "payment amount is negative", "payments")

    log.info("%d payment(s) have a missing amount; kept as null, excluded from revenue",
             int(payments["amount_is_missing"].sum()))

    return payments.reset_index(drop=True)
