"""Single source of truth for paths, business rules and constants.

Owns:   every magic value used anywhere in the pipeline.
Called by: every other module in src/.
Never:  reads or writes data, imports another src module.
"""

import os
from pathlib import Path

# --- Paths ---------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"

SOURCE_WORKBOOK = DATA_DIR / "raw_UseCase-Airlines.xlsx"
BRONZE_DIR = DATA_DIR / "bronze"          # raw sheets, as extracted, untouched
SILVER_DIR = DATA_DIR / "silver"          # cleaned, validated, PII-protected
GOLD_DIR = DATA_DIR / "gold"              # star schema + KPI tables for Power BI
QUARANTINE_DIR = DATA_DIR / "quarantine"  # rejected records + why they were rejected

SHEETS = ["flights", "bookings", "passengers", "payments"]

# --- Expected schema -----------------------------------------------------
# Checked at ingestion. A missing column stops the run; an extra column is a warning.
EXPECTED_COLUMNS = {
    "flights": ["flight_id", "airline", "source", "destination",
                "departure_time", "arrival_time", "duration"],
    "bookings": ["booking_id", "passenger_id", "flight_id", "booking_date", "status",
                 "passport_number", "seat_number",
                 "emergency_contact_name", "emergency_contact_phone"],
    "passengers": ["passenger_id", "first_name", "last_name", "age", "gender",
                   "email", "phone", "aadhaar_id", "date_of_birth"],
    "payments": ["payment_id", "booking_id", "amount", "payment_method"],
}

# --- Business rules ------------------------------------------------------
# The flight_id prefix identifies the carrier. Verified conflict-free against all
# 1020 source rows before use (see clean.repair_airline_from_flight_id).
PREFIX_TO_AIRLINE = {
    "AI": "Air India",
    "UK": "Vistara",
    "SJ": "SpiceJet",
    "6F": "IndiGo",
}

VALID_AIRPORTS = ["BOM", "DEL", "CCU", "HYD", "MAA", "BLR"]

# Values that mean "we do not know", written as if they were real data.
NULL_TOKENS = ["UNKNOWN", "INVALID", "NA", "N/A", "NULL", "", "NAN", "NONE"]

VALID_BOOKING_STATUSES = ["CONFIRMED", "CANCELLED", "PENDING"]
UNKNOWN_STATUS = "UNKNOWN"

# Plausible duration window for an Indian domestic sector. Outside this range a
# flight is flagged as an anomaly for review - it is never deleted.
MIN_PLAUSIBLE_DURATION_MIN = 20
MAX_PLAUSIBLE_DURATION_MIN = 480

# Duration outliers are found per route with the standard Tukey fence: a flight is
# an outlier if it sits more than 1.5 x IQR outside its own route's quartiles.
# A fixed minute tolerance was tried first and rejected - see DECISIONS.md D-009.
ROUTE_OUTLIER_IQR_MULTIPLIER = 1.5

# --- Privacy -------------------------------------------------------------
# Salt lives outside the repo. The documented default keeps local runs reproducible
# for a reviewer who has no .env file; it is not a production secret.
PII_SALT = os.getenv("ASG_PII_SALT", "asg-airlines-local-dev-salt")
HASH_PREFIX_LENGTH = 16  # characters of the SHA-256 hex digest kept as the token
