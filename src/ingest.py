"""Stage 1 - read the source workbook into memory and land it in bronze.

Owns:   getting bytes off disk and into DataFrames. Nothing else.
Called by: pipeline.run()
Never:  cleans, repairs or reshapes data. Bronze is a faithful copy of the source.
"""

import logging

import pandas as pd

from src import config

log = logging.getLogger(__name__)


def load_workbook(path=None):
    """Read every expected sheet as raw text and return {sheet_name: DataFrame}.

    Everything is read as string (dtype=str) on purpose. Letting pandas guess types
    here would silently coerce corrupt values - a malformed timestamp would become
    NaT before validate.py ever gets to see it and record why it was rejected.
    Typing happens later, in transform.py, where failures can be quarantined.
    """
    path = path or config.SOURCE_WORKBOOK
    log.info("Reading workbook: %s", path)

    frames = {}
    for sheet in config.SHEETS:
        df = pd.read_excel(path, sheet_name=sheet, dtype=str)
        df = df.dropna(axis=1, how="all")   # source has trailing all-empty columns
        df.columns = [c.strip() for c in df.columns]
        frames[sheet] = df
        log.info("  %-11s %5d rows x %d columns", sheet, len(df), df.shape[1])

    return frames


def write_bronze(frames):
    """Persist the untouched extracts so any later result can be traced to its input."""
    config.BRONZE_DIR.mkdir(parents=True, exist_ok=True)
    for name, df in frames.items():
        df.to_csv(config.BRONZE_DIR / f"{name}.csv", index=False)
    log.info("Bronze written: %s", config.BRONZE_DIR)
