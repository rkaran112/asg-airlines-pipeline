"""Stage 2 - schema checks, referential checks, and the quarantine ledger.

Owns:   deciding whether a record is fit to continue, and recording why not.
Called by: pipeline.run(), and by transform.py when a timestamp is impossible.
Never:  changes a value. Validation only ever splits rows into keep / reject.

Rejected rows are never silently dropped. Every one lands in
data/quarantine/quarantine.csv with the sheet it came from and the rule that
rejected it, so the row counts in the final report always reconcile:

    rows_in = rows_out + rows_quarantined
"""

import logging

import pandas as pd

from src import config

log = logging.getLogger(__name__)

# The ledger. Appended to by reject(), written once at the end of the run.
_quarantine = []


class SchemaError(Exception):
    """Raised when the source workbook no longer matches the contract in config."""


def check_schema(frames):
    """Fail the run if any expected column is missing; warn on unexpected extras."""
    for sheet, expected in config.EXPECTED_COLUMNS.items():
        if sheet not in frames:
            raise SchemaError(f"Sheet '{sheet}' is missing from the workbook")

        actual = list(frames[sheet].columns)
        missing = [c for c in expected if c not in actual]
        extra = [c for c in actual if c not in expected]

        if missing:
            raise SchemaError(f"Sheet '{sheet}' is missing columns: {missing}")
        if extra:
            log.warning("Sheet '%s' has unexpected extra columns: %s", sheet, extra)

    log.info("Schema check passed for all %d sheets", len(config.EXPECTED_COLUMNS))


def reject(df, mask, reason, source):
    """Split `df` on `mask`: rejected rows go to the ledger, the rest are returned.

    `mask` is True for rows that FAIL. Returns only the surviving rows.
    """
    if mask.sum() == 0:
        return df

    rejected = df[mask].copy()
    rejected["_rejection_reason"] = reason
    rejected["_source_sheet"] = source
    _quarantine.append(rejected)

    log.warning("Quarantined %d row(s) from %s: %s", int(mask.sum()), source, reason)
    return df[~mask].copy()


def check_referential_integrity(bookings, flights, passengers, payments):
    """Drop rows whose foreign key does not resolve, so joins can never fan out."""
    bookings = reject(
        bookings, ~bookings["flight_id"].isin(flights["flight_id"]),
        "flight_id does not exist in the flights dimension", "bookings")
    bookings = reject(
        bookings, ~bookings["passenger_id"].isin(passengers["passenger_id"]),
        "passenger_id does not exist in the passengers dimension", "bookings")
    payments = reject(
        payments, ~payments["booking_id"].isin(bookings["booking_id"]),
        "booking_id does not exist in the bookings fact", "payments")
    return bookings, payments


def write_quarantine():
    """Flush the ledger to disk and return how many rows were rejected in total."""
    config.QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)
    path = config.QUARANTINE_DIR / "quarantine.csv"

    if not _quarantine:
        pd.DataFrame(columns=["_rejection_reason", "_source_sheet"]).to_csv(path, index=False)
        log.info("Quarantine: 0 rows rejected")
        return 0

    ledger = pd.concat(_quarantine, ignore_index=True)
    ledger.to_csv(path, index=False)
    log.info("Quarantine: %d rows rejected -> %s", len(ledger), path)
    for reason, count in ledger["_rejection_reason"].value_counts().items():
        log.info("    %3d  %s", count, reason)
    return len(ledger)


def reset():
    """Clear the ledger. Only needed when the pipeline runs twice in one process."""
    _quarantine.clear()
