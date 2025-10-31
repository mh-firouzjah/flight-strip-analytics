import datetime as dt
import uuid

import polars as pl


def _transform_df_date_time_values(df: pl.DataFrame, year: int) -> pl.DataFrame:
    """Normalize and convert raw Excel data into typed datetime columns.

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
        raise Exception(f"Invalid Excel file contents. {err_msg}") from err


def _deduplicate_dataframe(df: pl.DataFrame) -> pl.DataFrame:
    """Deduplicate strips and remove junk rows.

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


def refine_df(df: pl.DataFrame, year: int) -> pl.DataFrame:
    utc_now = dt.datetime.now(dt.timezone.utc)
    refined_df = (
        df.pipe(_transform_df_date_time_values, year=year).pipe(_deduplicate_dataframe)
        # .pipe(_normalize_sector_column)  # Uncomment if sector normalization is desired
    ).with_columns([pl.lit(utc_now).alias("created"), pl.lit(utc_now).alias("updated")])
    return refined_df


def compute_journeys_and_sector_end(
    df: pl.DataFrame, MAX_CONTINUATION_GAP: dt.timedelta = dt.timedelta(hours=1)
) -> pl.DataFrame:
    flight_id_cols = ["callsign", "dep", "des"]

    # Ensure 'id' column exists for joining, if not, create it.
    if "id" not in df.columns:
        df = df.with_row_index("id")

    # Ensure journey-related columns exist, filling with null for new strips.
    for col in ["journey_id", "is_journey_start"]:
        if col not in df.columns:
            df = df.with_columns(
                pl.lit(None, dtype=pl.Utf8 if col == "journey_id" else pl.Boolean).alias(col)
            )

    # Step 1: Initialize state.
    # `db_strips` are from the day before the new data starts.
    db_strips = df.filter(pl.col("journey_id").is_not_null())
    new_strips_df = df.filter(pl.col("journey_id").is_null())

    if new_strips_df.is_empty():
        return db_strips  # No new strips to process, return original DB data.

    sorted_dates = new_strips_df.select(pl.col("flight_date").unique().sort()).to_series()
    processed_daily_dfs = []
    journeys_from_previous_day = db_strips

    # Step 2: Iterate over each day in the new data.
    for current_date in sorted_dates:
        strips_for_today = new_strips_df.filter(pl.col("flight_date") == current_date)

        # Step 2a: Identify continuations from the previous day's journeys.
        continued_strips = pl.DataFrame()
        if not journeys_from_previous_day.is_empty():
            # Find the last strip of each journey from the previous day.
            last_strips_of_yesterday = (
                journeys_from_previous_day.sort("point1time")
                .group_by("journey_id", maintain_order=True)
                .last()
                .select(flight_id_cols + ["point1time", "journey_id"])
                .rename({"point1time": "last_point1time"})
            )

            # Join today's strips with yesterday's last strips to find potential continuations.
            potential_continuations = strips_for_today.join(
                last_strips_of_yesterday, on=flight_id_cols, how="left"
            )

            # A strip is a continuation if the time gap is acceptable.
            is_continuation = (pl.col("point1time") - pl.col("last_point1time")) <= MAX_CONTINUATION_GAP

            # Assign the inherited journey_id to the continued strips.
            continued_strips = potential_continuations.filter(is_continuation).with_columns(
                pl.col("journey_id"),  # This is the inherited journey_id from the join
                pl.lit(False).alias("is_journey_start"),
            )

        # Step 2b: Identify new journeys starting today.
        # These are the strips that were NOT identified as continuations.
        if not continued_strips.is_empty():
            new_journeys_today = strips_for_today.join(continued_strips.select("id"), on="id", how="anti")
        else:
            new_journeys_today = strips_for_today

        if not new_journeys_today.is_empty():
            # Sort to process chronologically and identify journey starts.
            new_journeys_today = new_journeys_today.sort(flight_id_cols + ["point1time"])

            identifier_changed = pl.any_horizontal(pl.col(c) != pl.col(c).shift(1) for c in flight_id_cols)
            time_gap_too_large = pl.col("point1time").diff() > MAX_CONTINUATION_GAP

            new_journeys_today = new_journeys_today.with_columns(
                (identifier_changed | time_gap_too_large).fill_null(True).alias("is_journey_start")
            )

            # Assign unique journey IDs to these new journeys using a cum_sum trick.
            new_journey_groups = new_journeys_today.with_columns(
                pl.col("is_journey_start").cum_sum().alias("journey_idx")
            ).select(["id", "journey_idx"])

            # Create a map from the temporary index to a new UUID.
            unique_journey_indices = new_journey_groups.select("journey_idx").unique()
            uuid_map = pl.DataFrame(
                {
                    "journey_idx": unique_journey_indices.get_column("journey_idx"),
                    "journey_id_new": [str(uuid.uuid4()) for _ in range(len(unique_journey_indices))],
                }
            )

            # Join the new UUIDs back to the DataFrame.
            new_journeys_today = (
                new_journeys_today.join(new_journey_groups, on="id")
                .join(uuid_map, on="journey_idx")
                .with_columns(pl.col("journey_id_new").alias("journey_id"))
                .drop(["journey_idx", "journey_id_new"])
            )

        # Step 2c: Combine all strips for today and update state for the next day.
        all_strips_today = pl.concat(
            [continued_strips.select(new_journeys_today.columns), new_journeys_today]
        )
        processed_daily_dfs.append(all_strips_today)
        journeys_from_previous_day = all_strips_today

    # Step 3: Combine all processed daily DataFrames.
    if not processed_daily_dfs:
        return db_strips  # Should not happen if new_strips_df was not empty, but a safe guard.

    final_df = pl.concat([db_strips] + processed_daily_dfs, how="vertical_relaxed")

    # Step 4: Compute last valid point time for each strip.
    final_df = final_df.with_columns(
        pl.coalesce(
            [
                pl.when(~pl.col(f"point{i}").str.starts_with("OI")).then(pl.col(f"point{i}time"))
                for i in range(6, 0, -1)
            ]
            + [pl.col("point1time")]
        ).alias("last_point_time")
    )

    # Step 5: Compute sector_end for each strip within its journey.
    final_df = final_df.sort(["journey_id", "point1time"])
    next_strip_point1time = pl.col("point1time").shift(-1).over("journey_id")

    final_df = final_df.with_columns(
        next_strip_point1time.fill_null(pl.col("last_point_time")).dt.round("1s").alias("sector_end")
    ).drop("last_point_time")

    return final_df
