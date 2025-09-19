"""Excel parsing module using Polars and fastexcel backend.

Ensures schema consistency, validation, and sanitization.
"""

import datetime as dt
import logging
import uuid

import polars as pl
from django.conf import settings

logger = logging.getLogger(__name__)

MAX_CONTINUATION_GAP = pl.duration(
    hours=settings.MAX_CONTINUATION_GAP_HOURS,
    minutes=settings.MAX_CONTINUATION_GAP_MINUTES,
)
SCHEMA_OVERRIDES = {
    "CALLSIGN": pl.String,
    "SSRCODE": pl.String,
    "DATE": pl.String,
    "Register": pl.String,
    "DEP": pl.String,
    "DES": pl.String,
    "AIRCRAFTNAME": pl.String,
    "AIRCRAFTTYPE": pl.String,
    "Route": pl.String,
    "POINT1": pl.String,
    "POINT1TIME": pl.String,
    "POINT2": pl.String,
    "POINT2TIME": pl.String,
    "POINT3": pl.String,
    "POINT3TIME": pl.String,
    "POINT4": pl.String,
    "POINT4TIME": pl.String,
    "POINT5": pl.String,
    "POINT5TIME": pl.String,
    "POINT6": pl.String,
    "POINT6TIME": pl.String,
    "SPEED": pl.String,
    "CFL": pl.String,
    "RFL": pl.String,
    "sector": pl.String,
}

INVALID_BADGE_START = '<span style="color: red; font-weight: bold;">'
INVALID_BADGE_END = "</span>"
MISSING_BADGE = f"{INVALID_BADGE_START}MISSING{INVALID_BADGE_END}"

# Required columns must be non-null + match regex
REQUIRED_VALIDATION_RULES = {
    "callsign": r"^[A-Z\d]{2,7}$",
    "date": r"\d{1,2}[A-Z]{3}",  # we can parse separately for %d%b
    "dep": r"^[A-HK-WYZ][A-Z]{3}$",
    "des": r"^[A-HK-WYZ][A-Z]{3}$",
    "point1": r"^([A-Z]{2,5})|(ZZ\d\d([A-Z]?|[A-Z]\d?))$",
    "point1time": r"^\d{4}$",
    "sector": r"^S\d{1,2}$",
}

# Optional columns may be null, but if present must match regex
OPTIONAL_VALIDATION_RULES = {
    **{f"point{i}": r"^([A-Z]{2,5})|(ZZ\d\d([A-Z]?|[A-Z]\d?))$" for i in range(2, 7)},
    **{f"point{i}time": r"^\d{4}$" for i in range(2, 7)},
    "ssrcode": r"^\d{4}$",
}


class NoDataError(Exception):
    """Raised when an uploaded Excel file contains no rows to import."""

    pass


def _format_invalid_badge(col_expr: pl.Expr) -> pl.Expr:
    """Helper to create the invalid badge expression correctly."""
    return pl.lit(INVALID_BADGE_START) + col_expr + pl.lit(INVALID_BADGE_END)


def validate_date_col(col_expr: pl.Expr, year: int) -> pl.Expr:
    """Validate 'date' column in format '%d%b'. Null is treated as missing."""
    col_with_year = col_expr + pl.lit(str(year))
    parsed = col_with_year.str.to_date(format="%d%b%Y", strict=False)
    return (
        pl.when(col_expr.is_null())
        .then(pl.lit(MISSING_BADGE))
        .when(parsed.is_null())
        .then(_format_invalid_badge(col_expr))
        .otherwise(col_expr)
    )


def validate_point_N_time_col(col_name: str, regex: str) -> pl.Expr:
    """Validate 'point_N_time' columns."""
    col_expr = pl.col(col_name)
    point_col_expr = pl.col(col_name[:6])  # e.g., 'point1' from 'point1time'

    return (
        pl.when(col_expr.is_not_null() & (~col_expr.str.contains(regex)))
        .then(_format_invalid_badge(col_expr))
        # This checks if a time exists without its corresponding point
        .when(point_col_expr.is_null() & col_expr.is_not_null())
        .then(_format_invalid_badge(col_expr))
        .otherwise(col_expr)
        .alias(col_name)
    )


def validate_df(df: pl.DataFrame, year: int) -> pl.DataFrame:
    """Applies all validation rules to the DataFrame."""
    exprs = []

    # Required columns: must be non-null + match regex
    for col, regex in REQUIRED_VALIDATION_RULES.items():
        if col not in df.columns:
            continue
        col_expr = pl.col(col)
        if col == "date":
            exprs.append(validate_date_col(col_expr, year).alias("date"))
        else:
            exprs.append(
                pl.when(col_expr.is_null())
                .then(pl.lit(MISSING_BADGE))
                .when(~col_expr.str.contains(regex))
                .then(_format_invalid_badge(col_expr))
                .otherwise(col_expr)
                .alias(col)
            )
    # Optional columns: allow null, but validate if present
    for col, regex in OPTIONAL_VALIDATION_RULES.items():
        if col not in df.columns:
            continue
        if "time" in col:
            exprs.append(validate_point_N_time_col(col, regex))
        else:
            col_expr = pl.col(col)
            exprs.append(
                pl.when(col_expr.is_not_null() & (~col_expr.str.contains(regex)))
                .then(_format_invalid_badge(col_expr))
                .otherwise(col_expr)
                .alias(col)
            )

    return df.with_columns(exprs)


def _load_and_validate_excel(excel_file: bytes, year: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Load an Excel file into a Polars DataFrame with schema enforcement and validation.

    This function:
    - Reads the Excel file using the `calamine` backend for speed and compatibility.
    - Applies `SCHEMA_OVERRIDES` to ensure expected dtypes.
    - Normalizes column names to lowercase.
    - Replaces empty strings with `null`.
    - Validates that required columns are present and non-empty.
    - Validates that `sector` names conform to "S" followed by 1–2 digits.

    Parameters
    ----------
    excel_file : io.BytesIO
        A byte buffer containing Excel file contents.

    Returns
    -------
    pl.DataFrame
        Parsed and validated DataFrame ready for further processing.
    pl.DataFrame
        `errors_df` contains original row data plus an 'error' column.
    Raises
    ------
    HTTPException
        If file is empty, malformed, missing columns, or contains invalid values.
    """

    try:
        df = pl.read_excel(
            excel_file,
            has_header=True,
            engine="calamine",
            raise_if_empty=True,
            schema_overrides=SCHEMA_OVERRIDES,
        )
        df.columns = [col.lower().strip() for col in df.columns]
    except pl.exceptions.NoDataError as err:
        raise NoDataError("The uploaded file contains no data.") from err
    except KeyError as err:
        raise NoDataError(f"Missing required column: {err}. Please check the file template.") from err
    except Exception as err:
        raise NoDataError(f"Invalid Excel file: {err}") from err

    # Replace empty strings with null
    df = df.with_columns(pl.col(c).str.strip_chars().replace("", None).alias(c) for c in df.columns)
    df = df.with_row_index("row_nr", 1)

    validated_df = validate_df(df, year)

    cols_to_check = (*REQUIRED_VALIDATION_RULES.keys(), *OPTIONAL_VALIDATION_RULES.keys())

    # Build a list of boolean expressions, fill_null(False) is crucial
    error_condition_exprs = [
        pl.col(c).str.contains(INVALID_BADGE_START).fill_null(False) for c in cols_to_check
    ]

    # Use pl.any_horizontal to check if any of the conditions are true for a given row
    errors_mask = validated_df.select(pl.any_horizontal(error_condition_exprs)).to_series()

    errors_df = validated_df.filter(errors_mask)
    valid_df = validated_df.filter(~errors_mask)
    return valid_df.drop("row_nr"), errors_df


def _transform_df_date_time_values(df: pl.DataFrame, year: int) -> pl.DataFrame:
    """
    Normalize and convert raw Excel data into typed datetime columns.

    This function performs date and time conversions three passes for
    robustness and consistency:

    - Converts the string `date` column (format like "02JUL") into a proper
      `pl.Date`, using the provided `year`.
    - Builds a UTC-aware base datetime (`base_dt_utc`) for each row.
    - Converts each column in `point{i}time` (HHMM format, may be missing leading zeros)
      into a `pl.Datetime` in UTC.
    - Handles wrong date of tomorrow strips cause of print 15min to sector boundary:
      if the computed time is ≤ 00:15 and the first point (`point1`) starts
      with "ZZ", the datetime is shifted forward by one day.
    -Adjust time columns for midnight rollover:
      For each POINT1..6TIME column after the first, if its time is earlier than
      the base (point1time), it is assumed to belong to the following day and
      incremented by 1 day.

    On failure, raises an HTTP 422 error with a user-friendly message identifying
    the problematic column.

    Parameters
    ----------
    df : pl.DataFrame
        Input dataframe containing at least `date`, `point1`, and all `point{i}time`.
    year : int
        Reference year to disambiguate `%d%b` dates (e.g., "02JUL" → "2025-07-02").

    Returns
    -------
    pl.DataFrame
        A copy of the input with normalized `date` and all `point{i}time` converted
        into UTC `Datetime` columns.

    Raises
    ------
    HTTPException
        If type conversion fails (invalid formats, corrupted Excel values, etc.).

    Notes
    -----
    - All operations are vectorized and executed lazily by Polars.
    - Performs one `with_columns` pass for date and one for time columns to keep
      error messages specific.
    - Timezone conversions assume input times are naive local and normalize them
      to UTC.
    """
    TIME_COLS = [f"point{i}time" for i in range(1, 7)]

    date_expr = (pl.col("date").str.to_uppercase() + str(year)).str.to_date(format="%d%b%Y").alias("date")

    base_dt_utc = pl.col("date").cast(pl.Datetime).dt.convert_time_zone("UTC")
    zz_mask = pl.col("point1").str.starts_with("ZZ")
    time_exprs: list[pl.Expr] = []
    for col in TIME_COLS:
        hhmm = pl.col(col).cast(pl.UInt16).fill_null(0)
        dt_expr = base_dt_utc + pl.duration(hours=(hhmm // 100), minutes=(hhmm % 100))

        # Fix SINA tomorrow strip print 15min to sector boundary
        if col == "point1time":
            dt_expr = pl.when((hhmm < 16) & zz_mask).then(dt_expr + pl.duration(days=1)).otherwise(dt_expr)

        time_exprs.append(dt_expr.dt.round("1s").alias(col))

    rollover_exprs = [
        pl.when(pl.col(col) < pl.col("point1time"))
        .then(pl.col(col) + pl.duration(days=1))
        .otherwise(pl.col(col))
        .dt.round("1s")
        .alias(col)
        for col in TIME_COLS[1:]  # Only for point2time onwards
    ]

    try:
        df = df.with_columns(date_expr)
        df = df.with_columns(time_exprs)
        df.with_columns(rollover_exprs)
        return df.with_columns(pl.col("point1time").dt.date().alias("flight_date"))
    except (pl.exceptions.ComputeError, pl.exceptions.InvalidOperationError) as err:
        err_msg = str(err).splitlines()[0].capitalize().replace("u16", "datetime")
        logger.warning(f"Invalid Excel file contents. {err_msg}")
        raise Exception(f"Invalid Excel file contents. {err_msg}") from err


def _deduplicate_dataframe(df: pl.DataFrame) -> pl.DataFrame:
    """
    Deduplicate strips and remove junk rows.

    SINA prints strips weirdly:
        1. Sometimes we have two strips for a sector one of which point1 name starts with ZZ
        and the other is a compulsory point, in such cases we drop ZZ one
        2. Sometimes there are more than one point as above which their point1 starts with ZZ
        but there is no strip with a compulsory point1, so we have to drop one of those ZZ points
        3. Sometimes it prints two strips for a sector either at the begining of journey or at the
        very end of it, e.g departure from OIBK has 2 strips one starts with OIBK other starts with KIS
        or traffic exiting FIR via NAZAR has one strip for NAZAR and one other for ZZ07E as sector boundary point
        4. sometimes it prints a single strip multiple times with no diff at all

    Parameters
    ----------
    df : pl.DataFrame
        Input dataframe.

    Returns
    -------
    pl.DataFrame
        Cleaned DataFrame with duplicates and junk removed.
    """
    # Flag suspected junk strips
    df = df.with_columns(pl.col("point1").str.starts_with("ZZ").alias("is_junk"))

    # Compute per-group whether there is any non-junk row
    # Group key: callsign, date, dep, des, sector
    keep_group_flag = df.group_by(["callsign", "date", "dep", "des", "sector"]).agg(
        (~pl.col("is_junk")).any().alias("has_non_junk")
    )

    # Join flag back to original df
    df = df.join(keep_group_flag, on=["callsign", "date", "dep", "des", "sector"])

    # Keep row if either:
    # - not junk
    # - junk but no non-junk exists in the group
    df = df.filter((~pl.col("is_junk")) | (~pl.col("has_non_junk")))

    # Dedup only within junk rows, others untouched
    junk = (
        df.filter(pl.col("is_junk"))
        .sort("point1time")
        .unique(subset=["callsign", "date", "dep", "des", "sector"], keep="first")
    )
    non_junk = df.filter(~pl.col("is_junk"))
    # Combine back and drop helper columns
    df = pl.concat([non_junk, junk], how="vertical").drop(["is_junk", "has_non_junk"])

    # Filter: drop rows where point1 length == 4 AND group has more than 1 row
    group_size = df.group_by(["callsign", "date", "dep", "des", "sector"]).agg(pl.count().alias("group_size"))
    df = df.join(group_size, on=["callsign", "date", "dep", "des", "sector"])
    df = df.filter(~((pl.col("point1").str.len_chars() == 4) & (pl.col("group_size") > 1))).drop("group_size")

    return df.unique(subset=["callsign", "date", "dep", "des", "sector", "point1"], keep="first")


def _normalize_sector_column(df: pl.DataFrame) -> pl.DataFrame:
    """Normalize the `sector` column: remove leading 'S' or 's' and convert to an integer."""
    if "sector" not in df.columns:
        return df

    # Strip leading 'S' or 's', trim whitespace and cast to integer.
    return df.with_columns(
        pl.col("sector")
        .str.replace(r"^[Ss]", "", literal=False)
        .str.strip_chars()
        .cast(pl.Int32)
        .alias("sector")
    )


def compute_journeys_and_sector_end(df: pl.DataFrame) -> pl.DataFrame:
    """
    Compute `sector_end` for each strip within continuous journeys.

    This function:
    - Groups strips into continuous journeys using a 4-hour max gap rule.
    - Uses the next strip’s entry time (`point1time`) as the current strip’s sector end.
    - Falls back to the last non-"OI" point time if no next strip exists.
    - Adds a temporary `last_point` column (for debugging/inspection).

    Parameters
    ----------
    df : pl.DataFrame
        DataFrame with datetime-normalized strips.

    Returns
    -------
    pl.DataFrame
        DataFrame with an added `sector_end` column.

    Notes
    -----
    - Uses a continuation threshold (`MAX_CONTINUATION_GAP`).
    - Drops helper columns (`is_journey_start`, `last_point_time`, `journey_id`).
    """

    # --- The part below needs more checks ---
    FLIGHT_ID_COLS = ["callsign", "dep", "des"]
    # Step 1: Sort the data to establish a clear, chronological order.
    # Sorting by 'date' first is crucial to ensure we process each day as a separate block.
    sort_columns = ["date"] + FLIGHT_ID_COLS + ["point1time"]
    df = df.sort(sort_columns)

    # Step 2: Define the conditions for a new journey to start.
    # We check if the primary identifiers for a daily journey have changed from the previous row.
    daily_journey_identifiers = ["date"] + FLIGHT_ID_COLS
    identifier_changed = pl.any_horizontal(
        pl.col(daily_journey_identifiers) != pl.col(daily_journey_identifiers).shift(1)
    )

    # Step 2: Calculate the time difference from the previous strip *within the same flight group*.
    time_gap_too_large = pl.col("point1time").diff().over(daily_journey_identifiers) > MAX_CONTINUATION_GAP

    # Step 3: Identify the start of a new journey.
    # A journey starts if the time gap is too large, or if it's the very first
    # strip for that flight (where the gap is null).
    df = df.with_columns(
        (identifier_changed | time_gap_too_large)
        .fill_null(True)  # First strip in every group is always a new journey.
        .alias("is_journey_start")
    )
    # --- The part above needs more checks ---

    # Step 4: Assign a unique ID to each continuous journey.
    # The cumulative sum of a boolean series is a classic trick to create groups.
    df = df.with_columns(pl.col("is_journey_start").cum_sum().alias("journey_idx"))
    # Create a mapping DataFrame from journey_idx → UUID
    unique_journey_idx = df.select("journey_idx").unique().to_series()
    uuid_df = pl.DataFrame(
        {
            "journey_idx": unique_journey_idx,
            "journey_id": [str(uuid.uuid4()) for _ in range(unique_journey_idx.len())],
        }
    )
    df = df.join(uuid_df, on="journey_idx").drop("journey_idx")

    # Step 5: Compute last valid point time per row
    df = df.with_columns(
        pl.coalesce(
            [
                pl.when(~pl.col(f"point{i}").str.starts_with("OI")).then(pl.col(f"point{i}time"))
                for i in range(6, 0, -1)
            ]
            + [pl.col("point1time")]  # hard fallback if all points are OI/null
        ).alias("last_point_time")
    )

    # Step 7: Compute sector_end as next strip's point1time within the same journey & Drop helper column
    next_strip_point1time = pl.col("point1time").shift(-1).over("journey_id")
    df = df.with_columns(
        next_strip_point1time.fill_null(pl.col("last_point_time")).dt.round("1s").alias("sector_end")
    ).drop("last_point_time")
    return df


def process_excel_bytes(excel_bytes: bytes, year: int) -> tuple[pl.DataFrame | None, pl.DataFrame | None]:
    """
    End-to-end ETL pipeline for Excel flight strips.

    Steps:
    1. Load Excel into a typed Polars DataFrame with schema validation.
    2. Deduplicate and remove junk rows.
    3. Convert `date` and `point{i}time` cols into UTC datetimes.
    4. Fix midnight rollovers within a day.
    5. Compute `sector_end` for each strip.

    Parameters
    ----------
    excel : UploadFile
        Excel file uploaded via FastAPI.
    year : int
        Reference year to disambiguate `%d%b` date strings.

    Returns
    -------
    pl.DataFrame
        Cleaned, validated, and database-ready DataFrame of flight strips.
    pl.DataFrame
        `errors_df` contains original row data plus an 'error' column.
    """

    valid_df, errors_df = _load_and_validate_excel(excel_bytes, year)

    if valid_df.is_empty():
        return None, errors_df

    utc_now = dt.datetime.now(dt.timezone.utc)
    processed_df = (
        valid_df.pipe(_deduplicate_dataframe)
        .pipe(_transform_df_date_time_values, year=year)
        .pipe(_deduplicate_dataframe)
        .pipe(compute_journeys_and_sector_end)
        # .pipe(_normalize_sector_column)  # Uncomment if sector normalization is desired
    ).with_columns([pl.lit(utc_now).alias("created"), pl.lit(utc_now).alias("updated")])
    return processed_df, errors_df
