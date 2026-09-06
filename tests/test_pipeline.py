"""Tests for the logic that is easy to get silently wrong.

Deliberately narrow. There is no value in asserting that pandas can group a
DataFrame; these cover the four rules where a bug would produce plausible-looking
but incorrect numbers, plus the PII guard rail.

Run with:  python -m pytest tests -q
"""

import pandas as pd
import pytest

from src import clean, model, privacy, transform, validate


@pytest.fixture(autouse=True)
def _clear_quarantine():
    validate.reset()
    yield
    validate.reset()


def _flights(rows):
    """Build the minimum flight frame the transform chain expects."""
    df = pd.DataFrame(rows)
    df["has_conflicting_source_record"] = False
    df["airline_was_repaired"] = False
    return df


# --- overnight and duration ---------------------------------------------

def test_overnight_flight_gets_a_positive_duration():
    """23:38 to 02:32 next day is 174 minutes, not minus 1266."""
    flights = _flights([{
        "flight_id": "SJ010", "airline": "SpiceJet", "source": "CCU",
        "destination": "MAA", "departure_time": "2026-04-20 23:38:00",
        "arrival_time": "2026-04-21 02:32:00"}])

    out = transform.compute_duration(transform.parse_timestamps(flights))

    assert out.loc[0, "duration_minutes"] == 174
    assert out.loc[0, "is_overnight"]
    assert not out.loc[0, "arrival_date_was_repaired"]


def test_arrival_before_departure_is_repaired_by_one_day():
    """The SJ192 case: arrival dated a day early yields 300 minutes after repair."""
    flights = _flights([{
        "flight_id": "SJ192", "airline": "SpiceJet", "source": "HYD",
        "destination": "BOM", "departure_time": "2026-04-19 18:45:42",
        "arrival_time": "2026-04-18 23:45:42"}])

    out = transform.compute_duration(transform.parse_timestamps(flights))

    assert out.loc[0, "duration_minutes"] == 300
    assert out.loc[0, "arrival_date_was_repaired"]


def test_mixed_timestamp_formats_both_parse():
    """One row with microseconds, one without - neither may be lost."""
    flights = _flights([
        {"flight_id": "AI155", "airline": "Air India", "source": "BOM",
         "destination": "CCU", "departure_time": "2026-04-20 23:35:41.703000",
         "arrival_time": "2026-04-21 01:23:41.703000"},
        {"flight_id": "SJ149", "airline": "SpiceJet", "source": "DEL",
         "destination": "HYD", "departure_time": "2026-04-20 21:20:41.702000",
         "arrival_time": "2026-04-20 23:20:42"},
    ])

    out = transform.parse_timestamps(flights)

    assert len(out) == 2, "a valid row was quarantined for its timestamp format"
    assert out["arrival_time"].notna().all()


# --- airline repair ------------------------------------------------------

def test_missing_airline_is_recovered_from_the_flight_id_prefix():
    flights = pd.DataFrame({
        "flight_id": ["AI155", "UK094", "SJ010", "6F196"],
        "airline": ["Air India", None, None, "IndiGo"]})

    out = clean.repair_airline_from_flight_id(flights)

    assert out["airline"].tolist() == ["Air India", "Vistara", "SpiceJet", "IndiGo"]
    assert out["airline_was_repaired"].tolist() == [False, True, True, False]


def test_an_unseen_flight_id_prefix_stops_the_run():
    """Better to fail loudly than to mislabel a new carrier's flights."""
    flights = pd.DataFrame({"flight_id": ["ZZ001"], "airline": [None]})

    with pytest.raises(ValueError, match="not in PREFIX_TO_AIRLINE"):
        clean.repair_airline_from_flight_id(flights)


# --- the payment fan-out -------------------------------------------------

def test_bookings_do_not_fan_out_when_a_booking_has_several_payments():
    """Two payments on one booking must stay one booking row worth 300, not two."""
    bookings = pd.DataFrame({"booking_id": ["B1", "B2"], "status": ["CONFIRMED", "PENDING"]})
    payments = pd.DataFrame({
        "payment_id": ["P1", "P2", "P3"],
        "booking_id": ["B1", "B1", "B2"],
        "amount": [100.0, 200.0, 50.0],
        "amount_is_missing": [False, False, False]})

    fact = model.build_fact_booking(
        bookings, model.summarise_payments_by_booking(payments))

    assert len(fact) == 2
    assert fact.loc[fact.booking_id == "B1", "booking_total_amount"].iat[0] == 300.0
    assert fact.loc[fact.booking_id == "B1", "payment_count"].iat[0] == 2


def test_a_missing_amount_is_not_counted_as_zero():
    payments = pd.DataFrame({
        "payment_id": ["P1"], "booking_id": ["B1"],
        "amount": [None], "amount_is_missing": [True]})

    summary = model.summarise_payments_by_booking(payments)

    assert pd.isna(summary.loc[0, "booking_total_amount"])


# --- privacy -------------------------------------------------------------

def test_hashing_is_deterministic_and_does_not_leak_the_input():
    first, second = privacy.hash_value("433218196001"), privacy.hash_value("433218196001")

    assert first == second, "counts would break if the same id hashed differently"
    assert "433218196001" not in first
    assert privacy.hash_value("433218196002") != first


def test_the_guard_rail_catches_raw_pii_reaching_a_curated_table():
    leaky = pd.DataFrame({"passenger_id": ["P1"], "email": ["a@b.com"]})

    with pytest.raises(ValueError, match="Raw PII"):
        privacy.assert_no_raw_pii(leaky)


def test_masking_keeps_the_email_domain_but_hides_the_person():
    assert privacy.mask_email("vivaan.chatterjee@gmail.com") == "v***************e@gmail.com"


# --- quarantine ----------------------------------------------------------

def test_rejected_rows_are_recorded_rather_than_silently_dropped():
    df = pd.DataFrame({"flight_id": ["A", "B", "C"]})

    kept = validate.reject(df, df["flight_id"] == "B", "test rule", "flights")
    written = validate.write_quarantine()

    assert len(kept) == 2
    assert written == 1
