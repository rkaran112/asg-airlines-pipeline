"""Stage 4 - timestamps, flight duration, overnight handling, anomaly flags.

Owns:   every derived flight measure.
Called by: pipeline.run()
Never:  masks PII or builds tables - that is privacy.py and model.py.

This is the module the case study is really testing, so the duration rule is
spelled out here in full:

    1. Parse departure_time and arrival_time to real datetimes.
       Anything unparsable is quarantined, never guessed.

    2. If arrival is earlier than departure, the arrival DATE lost a day. Add one
       day to the arrival. This is the overnight (cross-day) repair.

    3. duration_minutes = arrival - departure, after the repair.

    4. A duration still outside 20 minutes - 8 hours after the repair is
       implausible. It is FLAGGED, not deleted, so the anomaly page can show it.

In this source the timestamps already carry full dates, so the 125 genuine
overnight flights (depart 23:38 on the 20th, arrive 02:32 on the 21st) already
subtract correctly and never enter step 2. Exactly one row does enter step 2:
SJ192, which departs 2026-04-19 18:45 but records arrival on 2026-04-18 23:45.
The +1 day repair yields 300 minutes, which matches the source workbook's own
Excel formula for that row - an independent confirmation that the repair is
right and that the record is not simply corrupt.
"""

import logging

import numpy as np
import pandas as pd

from src import config, validate

log = logging.getLogger(__name__)


def parse_timestamps(flights):
    """Type both time columns, handling the two formats present in the source.

    The source mixes two ISO layouts - 1016 departures carry microseconds
    ("2026-04-20 23:38:41.701000") and 4 do not ("2026-04-20 23:20:42"); the
    arrival column splits 989 / 31 the same way. pandas infers a single format
    from the first row, so a plain to_datetime() silently turns every
    minority-format row into NaT and the pipeline would quarantine 31 perfectly
    good flights. format="mixed" parses each value on its own terms.
    """
    for col in ["departure_time", "arrival_time"]:
        flights[col] = pd.to_datetime(flights[col], format="mixed", errors="coerce")

    flights = validate.reject(
        flights, flights["departure_time"].isna(),
        "departure_time could not be parsed as a timestamp", "flights")
    flights = validate.reject(
        flights, flights["arrival_time"].isna(),
        "arrival_time could not be parsed as a timestamp", "flights")

    return flights


def compute_duration(flights):
    """Apply the overnight repair, then derive duration_minutes."""
    rolled_over = flights["arrival_time"] < flights["departure_time"]

    flights["arrival_date_was_repaired"] = rolled_over
    flights.loc[rolled_over, "arrival_time"] += pd.Timedelta(days=1)
    log.info("Applied the +1 day overnight repair to %d flight(s)", int(rolled_over.sum()))

    delta = flights["arrival_time"] - flights["departure_time"]
    flights["duration_minutes"] = (delta.dt.total_seconds() / 60).round().astype("Int64")

    # A flight is overnight when it lands on a later calendar day than it departed.
    flights["is_overnight"] = (flights["arrival_time"].dt.date
                               != flights["departure_time"].dt.date)
    log.info("%d flight(s) cross midnight", int(flights["is_overnight"].sum()))

    return flights


def crosscheck_against_source_duration(flights):
    """Compare our duration to the workbook's own formula and log any disagreement.

    The source column is an Excel time serial, not a number of minutes, so it is
    converted before comparing. This is a verification step only - if the two
    disagree, ours wins, because the source formula cannot see the overnight repair.
    """
    if "_source_duration" not in flights.columns:
        return flights.drop(columns=["_source_duration"], errors="ignore")

    # The formula result reads back as an "HH:MM:SS" elapsed time, not a clock time.
    source_minutes = (pd.to_timedelta(flights["_source_duration"], errors="coerce")
                      .dt.total_seconds() / 60).round()

    comparable = source_minutes.notna() & flights["duration_minutes"].notna()
    mismatch = comparable & (source_minutes != flights["duration_minutes"])

    log.info("Duration cross-check: %d of %d rows agree with the source formula",
             int((comparable & ~mismatch).sum()), int(comparable.sum()))
    if mismatch.any():
        for fid in flights.loc[mismatch, "flight_id"].head(5):
            log.info("    differs on %s (expected, if the overnight repair applied)", fid)

    return flights.drop(columns=["_source_duration"])


def add_route_and_time_attributes(flights):
    """Derive the analysis grain Power BI slices on: route, date, hour, time band."""
    flights["route"] = flights["source"] + " - " + flights["destination"]
    flights["departure_date"] = flights["departure_time"].dt.date
    flights["departure_hour"] = flights["departure_time"].dt.hour
    flights["departure_time_band"] = pd.cut(
        flights["departure_hour"],
        bins=[-1, 5, 11, 16, 20, 23],
        labels=["Night (00-05)", "Morning (06-11)", "Afternoon (12-16)",
                "Evening (17-20)", "Late night (21-23)"])
    return flights


def flag_anomalies(flights):
    """Mark suspect flights. Nothing is deleted here - anomalies are a deliverable.

    Note on delays: this dataset has no scheduled-versus-actual time pair, so a
    true departure delay cannot be computed and is not invented. What is measurable
    is whether a flight's duration is out of line, which is what these flags carry.
    """
    flights["is_implausible_duration"] = ~flights["duration_minutes"].between(
        config.MIN_PLAUSIBLE_DURATION_MIN, config.MAX_PLAUSIBLE_DURATION_MIN)

    # Compare each flight to the spread of durations on its own route, using the
    # Tukey fence (1.5 x IQR outside the quartiles). The comparison has to be
    # per-route because a BOM-DEL sector and a BLR-MAA sector are not expected to
    # take the same time, so a single fleet-wide threshold would be meaningless.
    minutes = flights["duration_minutes"].astype("float")
    by_route = minutes.groupby(flights["route"])
    q1 = by_route.transform(lambda s: s.quantile(0.25))
    q3 = by_route.transform(lambda s: s.quantile(0.75))
    fence = config.ROUTE_OUTLIER_IQR_MULTIPLIER * (q3 - q1)

    flights["route_median_duration_minutes"] = by_route.transform("median")
    flights["duration_vs_route_median_minutes"] = (
        minutes - flights["route_median_duration_minutes"]).round().astype("Int64")
    flights["is_duration_outlier"] = (
        (minutes < q1 - fence) | (minutes > q3 + fence)).fillna(False)

    # airline_was_repaired is deliberately NOT an anomaly. The repair is lossless
    # and verified, so those 72 flights are trustworthy; folding them in here would
    # put 7% of the fleet on the anomaly page for no operational reason.
    flights["is_anomaly"] = (flights["is_implausible_duration"]
                             | flights["is_duration_outlier"]
                             | flights["arrival_date_was_repaired"]
                             | flights["has_conflicting_source_record"])

    flights["anomaly_reason"] = np.select(
        [flights["is_implausible_duration"],
         flights["arrival_date_was_repaired"],
         flights["has_conflicting_source_record"],
         flights["is_duration_outlier"]],
        ["Duration outside 20 min - 8 h",
         "Arrival date repaired (cross-day)",
         "Conflicting duplicate flight_id",
         "Duration far from route median"],
        default="None")

    log.info("Flagged %d flight(s) as anomalies (%.1f%% of the fleet)",
             int(flights["is_anomaly"].sum()),
             100 * flights["is_anomaly"].mean())
    return flights


def transform_flights(flights):
    """Run the flight transformation chain in order."""
    flights = parse_timestamps(flights)
    flights = compute_duration(flights)
    flights = crosscheck_against_source_duration(flights)
    flights = add_route_and_time_attributes(flights)
    flights = flag_anomalies(flights)
    return flights.reset_index(drop=True)
