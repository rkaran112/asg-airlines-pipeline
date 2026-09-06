"""The single entrypoint. Run with:  python -m src.pipeline

Owns:   the order of the stages and the run report.
Called by: the command line, and by the notebook.
Never:  contains business logic. Every rule lives in the stage module that owns it.

Reading this one function top to bottom tells you everything the pipeline does.
"""

import logging
import sys

from src import clean, config, ingest, kpis, model, privacy, transform, validate


def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)-14s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    return logging.getLogger("pipeline")


def run():
    """Ingest, validate, clean, transform, protect, model, aggregate."""
    log = configure_logging()
    validate.reset()
    log.info("=" * 78)
    log.info("ASG Airlines pipeline starting")
    log.info("=" * 78)

    # --- Stage 1: ingest -------------------------------------------------
    log.info("[1/7] Ingest")
    frames = ingest.load_workbook()
    ingest.write_bronze(frames)
    rows_in = {name: len(df) for name, df in frames.items()}

    # --- Stage 2: validate schema ---------------------------------------
    log.info("[2/7] Validate schema")
    validate.check_schema(frames)

    # --- Stage 3: clean --------------------------------------------------
    log.info("[3/7] Clean")
    flights = clean.clean_flights(frames["flights"])
    passengers = clean.clean_passengers(frames["passengers"])
    bookings = clean.clean_bookings(frames["bookings"])
    payments = clean.clean_payments(frames["payments"])

    # --- Stage 4: transform ----------------------------------------------
    log.info("[4/7] Transform")
    flights = transform.transform_flights(flights)

    # Referential integrity runs after transform, because transform can quarantine
    # a flight, which would orphan any booking that referenced it.
    bookings, payments = validate.check_referential_integrity(
        bookings, flights, passengers, payments)

    # --- Stage 5: protect PII --------------------------------------------
    log.info("[5/7] Protect PII")
    passengers = privacy.mask_passengers(passengers)
    bookings = privacy.mask_bookings(bookings)
    privacy.assert_no_raw_pii(flights, bookings, passengers, payments)
    write_silver(flights, bookings, passengers, payments)

    # --- Stage 6: model --------------------------------------------------
    log.info("[6/7] Model")
    tables = model.build_star_schema(flights, bookings, passengers, payments)
    model.write_gold(tables)

    # --- Stage 7: KPIs ---------------------------------------------------
    log.info("[7/7] KPIs")
    kpi_tables = kpis.build_all(tables)
    kpis.write_all(kpi_tables)

    rejected = validate.write_quarantine()
    report_reconciliation(log, rows_in, tables, rejected)

    log.info("Pipeline finished successfully")
    return tables, kpi_tables


def write_silver(flights, bookings, passengers, payments):
    """Persist the cleaned, PII-safe tables before they are reshaped into a star."""
    config.SILVER_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in [("flights", flights), ("bookings", bookings),
                     ("passengers", passengers), ("payments", payments)]:
        df.to_csv(config.SILVER_DIR / f"{name}.csv", index=False)


def report_reconciliation(log, rows_in, tables, rejected):
    """Prove that no row vanished without being accounted for."""
    log.info("-" * 78)
    log.info("Row reconciliation")
    log.info("  source rows        : %d", sum(rows_in.values()))
    for name, count in rows_in.items():
        log.info("      %-11s %5d", name, count)
    log.info("  curated fact/dim   : dim_flight %d, fact_booking %d, "
             "dim_passenger %d, fact_payment %d",
             len(tables["dim_flight"]), len(tables["fact_booking"]),
             len(tables["dim_passenger"]), len(tables["fact_payment"]))
    log.info("  quarantined        : %d  (see data/quarantine/quarantine.csv)", rejected)
    log.info("  deduplicated       : %d",
             sum(rows_in.values()) - rejected
             - len(tables["dim_flight"]) - len(tables["fact_booking"])
             - len(tables["dim_passenger"]) - len(tables["fact_payment"]))
    log.info("-" * 78)


if __name__ == "__main__":
    run()
