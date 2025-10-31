"""Excel parsing module using Polars and fastexcel backend.

Ensures schema consistency, validation, and sanitization.
"""

import polars as pl

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


def load_and_validate_excel(excel_bytes: bytes, year: int) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Load an Excel file into a Polars DataFrame with schema enforcement and validation.

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
            excel_bytes,
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
