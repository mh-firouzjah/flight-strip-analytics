"""
This module contains high-performance, raw SQL functions for bulk database
operations, bypassing the Django ORM where necessary for speed.
"""

import csv
import io
import logging
import uuid
from datetime import timedelta
from typing import NamedTuple

import polars as pl
from core.models import Strip
from django.conf import settings
from django.db.backends.utils import CursorWrapper

KEY_COLUMNS = ["callsign", "date", "dep", "des", "sector"]
DB_KEY_COLUMNS = [*KEY_COLUMNS, "id"]
NONKEY_COLUMNS = [f.name for f in Strip._meta.fields if f.name not in DB_KEY_COLUMNS]

logger = logging.getLogger(__name__)


class DbOp(NamedTuple):
    """Represents the outcome of a database upsert operation."""

    inserted: int
    updated: int
    imported: int
    unchanged: int


def fast_upsert_with_recovery(
    cursor: CursorWrapper,
    copy_statement: str,
    csv_buffer: io.StringIO,
    staging_table: str,
    df_columns: list[str],
):
    """
    Wrapper function to perform upsert with error handling and recovery.
    This function attempts to use the most efficient COPY method available
    on the given cursor's connection, falling back to less efficient methods
    if necessary. It logs any exceptions encountered during the process.
    Args:
        cursor: A database cursor for executing SQL commands.
        copy_statement: The SQL COPY command to execute.
        csv_buffer: An in-memory text buffer containing CSV data.
        staging_table: The name of the temporary staging table.
        df_columns: List of DataFrame columns to be copied.
    Returns:
        None
    Raises:
        Exception: Propagates any exception encountered during the COPY operation.
    """
    try:
        pg_cur = cursor.connection.cursor()
        # 1. Most performant: psycopg3's copy_expert
        if hasattr(pg_cur, "copy_expert"):
            pg_cur.copy_expert(copy_statement, csv_buffer)
        # 2. Next: psycopg3's copy context manager
        elif hasattr(pg_cur, "copy"):
            with pg_cur.copy(copy_statement) as copy_obj:
                copy_obj.write(csv_buffer.read())
        # 3. Next: Django's cursor.copy_from (if available)
        elif hasattr(cursor, "copy_from"):
            cursor.copy_from(csv_buffer, staging_table, sep=",", columns=df_columns)
        # 4. Least performant: batch INSERT using executemany
        else:
            csv_reader = csv.reader(csv_buffer)
            rows = [row for row in csv_reader]
            placeholders = ",".join(["%s"] * len(df_columns))
            insert_sql = f"INSERT INTO {staging_table} ({','.join(df_columns)}) VALUES ({placeholders})"
            pg_cur.executemany(insert_sql, rows)
    except Exception:
        logger.exception("COPY into staging table failed")
        raise


def upsert_strips_from_df(cursor: CursorWrapper, df: pl.DataFrame) -> DbOp:
    """
    Upserts flight strip data from a Polars DataFrame into the main Strip table
    using a high-performance staging table and raw SQL merge.

    This process is significantly faster than using the Django ORM for large
    datasets. It performs the following steps within a single transaction:

    1.  Creates a temporary staging table with the same structure as the main table.
    2.  Streams the DataFrame's data into the staging table using the highly
        efficient PostgreSQL COPY command.
    3.  Executes a single SQL INSERT ... ON CONFLICT statement to merge the data
        from the staging table into the main `core_strip` table.
    4.  The merge query identifies which rows are new (inserted), which have
        changed (updated), and which are identical (unchanged).
    5.  Calculates and returns the counts of these three outcomes.
    6.  The temporary table is automatically dropped at the end of the transaction.

    Args:
        df: A validated Polars DataFrame containing the strip data.

    Returns:
        A DbOp named tuple with counts for inserted, updated, unchanged and imported rows.
    """
    imported = df.height
    main_table = Strip._meta.db_table
    # Use a unique name for the temporary table to avoid any potential conflicts
    staging_table = f"staging_{main_table}_{uuid.uuid4().hex}"

    # Get model fields in database order to ensure CSV columns align perfectly
    db_columns = [*KEY_COLUMNS, *NONKEY_COLUMNS]
    db_columns_str = ", ".join(f'"{c}"' for c in db_columns)

    # Ensure DataFrame columns are in the exact same order as the database table
    df = df.select(db_columns)
    dup_keys = (
        df.group_by(["journey_id", "sector"])
        .len()  # or .agg(pl.len())
        .filter(pl.col("len") > 1)
        .select(["journey_id", "sector"])
    )

    # Step 2: filter original df to only those duplicates
    duplicate_rows = df.join(dup_keys, on=["journey_id", "sector"], how="inner")

    print(
        duplicate_rows.select(
            ["callsign", "date", "dep", "des", "sector", "point1", "point1time", "journey_id"]
        )
    )

    # 1. Create a temporary staging table, inheriting the structure of the main table
    cursor.execute(f"CREATE TEMP TABLE {staging_table} (LIKE {main_table} INCLUDING ALL) ON COMMIT DROP;")

    # 2. Stream DataFrame to an in-memory CSV file and use COPY for fast ingestion
    with io.StringIO() as csv_buffer:
        df_columns = df.columns
        df.write_csv(csv_buffer, include_header=False, null_value="\\N")
        del df
        csv_buffer.seek(0)
        # Use the low-level psycopg COPY API for streaming into the temp table.
        copy_statement = (
            f"COPY {staging_table} ({','.join(df_columns)}) FROM STDIN WITH "
            "(FORMAT csv, DELIMITER ',', NULL '\\N')"
        )
        fast_upsert_with_recovery(cursor, copy_statement, csv_buffer, staging_table, df_columns)

    # 3. Build and execute the MERGE (INSERT ... ON CONFLICT) query
    update_setters = ", ".join(f'"{col}" = EXCLUDED."{col}"' for col in NONKEY_COLUMNS)

    # The unique constraint fields
    conflict_target = ", ".join(f'"{c}"' for c in KEY_COLUMNS)

    # A tuple of all columns for the IS DISTINCT FROM check
    distinct_check_fields = [c for c in db_columns if c not in ("created", "updated")]
    all_cols_tuple = f"({', '.join(f'{main_table}."{c}"' for c in distinct_check_fields)})"
    excluded_tuple = f"({', '.join(f'excluded."{c}"' for c in distinct_check_fields)})"

    # The upsert SQL counts inserted vs updated rows using the system column `xmax`.
    # `xmax = 0` for an INSERT, non-zero for an UPDATE.
    # Rows that are identical (unchanged) do not trigger an UPDATE due to the
    # `IS DISTINCT FROM` check, and are counted by subtracting inserted+updated
    # from the total imported rows.
    upsert_sql = f"""
        WITH results AS (
            INSERT INTO {main_table} ({db_columns_str})
            SELECT {db_columns_str} FROM {staging_table}
            ON CONFLICT ({conflict_target})
            DO UPDATE SET {update_setters}
            -- This WHERE clause is crucial. It prevents an UPDATE for rows
            -- that are identical, allowing us to count them as "unchanged".
            WHERE {all_cols_tuple} IS DISTINCT FROM {excluded_tuple}
            -- xmax = 0 for an INSERT, non-zero for an UPDATE.
            RETURNING xmax
        )
        SELECT
            COUNT(*) FILTER (WHERE xmax = 0) AS inserted_count,
            COUNT(*) FILTER (WHERE xmax != 0) AS updated_count
        FROM results;
    """

    cursor.execute(upsert_sql)
    result = cursor.fetchone()
    inserted = result[0] if result else 0
    updated = result[1] if result else 0
    unchanged = imported - (inserted + updated)

    return DbOp(inserted=inserted, updated=updated, unchanged=unchanged, imported=imported)


def reconcile_journeys(df: pl.DataFrame):
    """
    Reconciles multi-day journeys by linking newly imported strips with
    ongoing journeys from the previous day.

    This function handles flight strips that span across midnight by performing
    the following steps:

    1. Identify new journey-start strips in the earliest date of the given DataFrame
       that fall before the configured `JOURNEY_RECONCILIATION_TIME_BOUNDARY`.
    2. Query the database for the last strip of any matching journey from the
       previous day (matching `callsign`, `dep`, `des`), keeping only the
       most recent strip per `journey_id`.
    3. Update the `sector_end` of these previous-day strips if the first strip
       of the new day starts before their current `sector_end`.
    4. Update the `journey_id` of new strips to match the corresponding previous-day
       journey for continuations.
       - **Note:** Currently, only the first strip is updated; all strips with
         the same temporary journey ID should be updated to maintain continuity.
    5. Return a tuple `(num_db_updates, updated_df)` where `num_db_updates`
       is the count of previous-day strips whose `sector_end` was modified,
       and `updated_df` is the reconciled Polars DataFrame ready for insertion.

    Args:
        df (pl.DataFrame): Newly imported flight strips, including columns:
            - callsign
            - dep
            - des
            - point1time
            - journey_id
            - is_journey_start
            - sector_end
            - ... other relevant columns

    Returns:
        Tuple[int, pl.DataFrame]:
            - Number of previous-day strips updated
            - Reconciled DataFrame including updated sector_end and journey_id
    """

    min_date = df.select(pl.col("point1time").dt.date()).min().item()
    boundary_df = df.filter(
        pl.col("is_journey_start").eq(True)
        & pl.col("point1time").dt.date().eq(min_date)
        & pl.col("point1time").dt.time().le(pl.time(hour=settings.JOURNEY_RECONCILIATION_TIME_BOUNDARY))
    )

    date_before_insert = min_date - timedelta(days=1)

    db_boundary_df = pl.DataFrame(
        list(
            Strip.objects.filter(
                date=date_before_insert,
                callsign__in=boundary_df["callsign"].unique().to_list(),
                dep__in=boundary_df["dep"].unique().to_list(),
                des__in=boundary_df["des"].unique().to_list(),
            )
            .order_by("journey_id", "-sector_end")  # order matters!
            .distinct("journey_id")
            .values(*(f.name for f in Strip._meta.fields if f.name != "id"))
        )
    )
    if db_boundary_df.is_empty():
        return 0, df

    # Step 1: identify old strips that need sector_end update
    updates_for_db = (
        db_boundary_df.join(boundary_df, on=["callsign", "dep", "des"], how="inner", suffix="_new")
        .filter(pl.col("point1time_new") < pl.col("sector_end"))
        .with_columns(
            pl.col("point1time_new").alias("sector_end")  # update sector_end
        )
        .select(db_boundary_df.columns)  # drop *_new
    )

    # Step 2: append updated old strips to original df
    df = pl.concat([updates_for_db, df.select(db_boundary_df.columns)], how="vertical")

    # Step 3: update journey_id for new strips that are continuations
    continuations = (
        boundary_df.join(db_boundary_df, on=["callsign", "dep", "des"], how="inner", suffix="_old").filter(
            pl.col("point1time") < pl.col("sector_end_old")
        )
        # .with_columns(pl.col("journey_id_old").alias("journey_id"))
        # .select(df.columns)  # drop *_old
    )

    df = (
        df.join(
            continuations.select(["callsign", "dep", "des", "journey_id", "journey_id_old"]),
            on=["callsign", "dep", "des", "journey_id"],
            how="left",
            suffix="_cont",
        )
        .with_columns(
            pl.when(pl.col("journey_id_old").is_not_null())
            .then(pl.col("journey_id_old"))
            .otherwise(pl.col("journey_id"))
            .alias("journey_id")
        )
        .select(df.columns)  # drop *_cont
    )

    return updates_for_db.height, df
