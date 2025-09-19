import sys
from pathlib import Path

from core.handlers.db import reconcile_journeys, upsert_strips_from_df
from core.handlers.excel import process_excel_bytes
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


class Command(BaseCommand):
    help = "Upload a flight strips Excel file and process it (same as admin form)."

    def add_arguments(self, parser):
        parser.add_argument(
            "file_path",
            type=str,
            help="Path to the Excel file to upload.",
        )
        parser.add_argument(
            "year",
            type=int,
            help="Year to append to 'date' column when parsing '%d%b' dates.",
        )

    def handle(self, *args, **options):
        file_path = options["file_path"]
        year = options["year"]

        if not Path(file_path).exists():
            raise CommandError(f"File not found: {file_path}")

        self.stdout.write(f"Loading and validating Excel file: {file_path} for year {year}...")
        valid_df, errors_df = process_excel_bytes(file_path, year)

        if valid_df is None or valid_df.is_empty():
            self.stdout.write("No valid rows to import. Check errors_df for issues.")
            sys.exit(1)

        if errors_df is not None and not errors_df.is_empty():
            self.stdout.write(f"{errors_df.height} invalid rows were found. They will be skipped.")

        sec_up, upsert_df = reconcile_journeys(valid_df)
        with connection.cursor() as cursor, transaction.atomic():
            result = upsert_strips_from_df(cursor, valid_df)
            self.stdout.write(
                f"Imported {result.imported} rows: {result.inserted} inserted, "
                f"{result.updated} updated, {sec_up} sector-end updates, {result.unchanged} unchanged."
            )
        self.stdout.write(self.style.SUCCESS("Upload and processing completed successfully."))
