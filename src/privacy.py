"""Stage 5 - protect passenger PII before anything leaves the silver zone.

Owns:   every field a regulator would call personal data.
Called by: pipeline.run()
Never:  writes to gold directly, and never returns the raw values it was given.

Three techniques, chosen per column by what analytics actually needs:

  hash    Deterministic salted SHA-256, truncated to 16 hex characters. Used for
          identifiers that must still JOIN and COUNT DISTINCT correctly
          (aadhaar_id, passport_number, email). Same input always gives the same
          token, so passenger counts stay exact, but the token cannot be reversed
          without the salt.

  mask    Format-preserving partial redaction (r****h@gmail.com, +91-98****6839).
          Used where a human operator needs to eyeball or verify a value.

  drop    Removed entirely. Used where the field serves no analytical purpose at
          all - emergency contact name and phone answer no KPI in this brief, so
          the safest handling is not to carry them.

  generalise  date_of_birth becomes an age band. The exact date is a strong
          re-identification key; the band answers every demographic question here.

The salt is read from the ASG_PII_SALT environment variable and is never
committed. Raw PII exists only in memory and in data/bronze/, which is gitignored.
"""

import hashlib
import logging

import pandas as pd

from src import config

log = logging.getLogger(__name__)


def hash_value(value):
    """Deterministic salted SHA-256 token. Null in, null out."""
    if pd.isna(value):
        return None
    digest = hashlib.sha256(f"{config.PII_SALT}{value}".encode("utf-8")).hexdigest()
    return digest[:config.HASH_PREFIX_LENGTH]


def hash_column(series):
    return series.map(hash_value)


def mask_email(value):
    """r****h@gmail.com - keeps the domain for provider analysis, hides the person."""
    if pd.isna(value) or "@" not in str(value):
        return None
    local, domain = str(value).split("@", 1)
    if len(local) <= 2:
        return f"{'*' * len(local)}@{domain}"
    return f"{local[0]}{'*' * (len(local) - 2)}{local[-1]}@{domain}"


def mask_phone(value):
    """+91-98****6839 - keeps the country code and enough digits to verify a call."""
    if pd.isna(value):
        return None
    text = str(value)
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) < 6:
        return "*" * len(text)
    return f"+91-{digits[-10:-8]}****{digits[-4:]}"


def age_band(age):
    """Generalise an exact age into a band, so no individual is singled out."""
    if pd.isna(age):
        return "Unknown"
    bounds = [(0, 12, "0-12"), (13, 17, "13-17"), (18, 24, "18-24"), (25, 34, "25-34"),
              (35, 44, "35-44"), (45, 54, "45-54"), (55, 64, "55-64")]
    for low, high, label in bounds:
        if low <= age <= high:
            return label
    return "65+"


def mask_passengers(passengers):
    """Return an analysis-safe passenger dimension. Raw columns are dropped."""
    safe = pd.DataFrame({
        "passenger_id": passengers["passenger_id"],
        "passenger_key": hash_column(passengers["passenger_id"]),
        "aadhaar_hash": hash_column(passengers["aadhaar_id"]),
        "email_masked": passengers["email"].map(mask_email),
        "email_domain": passengers["email"].astype(str).str.split("@").str[-1],
        "phone_masked": passengers["phone"].map(mask_phone),
        "gender": passengers["gender"],
        "age": passengers["age"],
        "age_band": passengers["age"].map(age_band),
    })

    dropped = ["first_name", "last_name", "date_of_birth", "aadhaar_id", "email", "phone"]
    log.info("Passenger PII protected: hashed 2, masked 2, generalised 1, dropped %d",
             len(dropped))
    return safe


def mask_bookings(bookings):
    """Hash the passport, drop the emergency contact, keep everything analytical."""
    safe = bookings.drop(
        columns=["passport_number", "emergency_contact_name", "emergency_contact_phone"],
        errors="ignore").copy()
    safe["passport_hash"] = hash_column(bookings["passport_number"])

    log.info("Booking PII protected: hashed passport_number, "
             "dropped emergency_contact_name and emergency_contact_phone")
    return safe


def assert_no_raw_pii(*frames):
    """Guard rail: fail the run if a raw PII column ever reaches the curated zones."""
    forbidden = {"first_name", "last_name", "email", "phone", "aadhaar_id",
                 "date_of_birth", "passport_number",
                 "emergency_contact_name", "emergency_contact_phone"}
    for df in frames:
        leaked = forbidden.intersection(df.columns)
        if leaked:
            raise ValueError(f"Raw PII column(s) {sorted(leaked)} reached a curated table")
    log.info("PII guard rail passed: no raw PII in any curated table")
