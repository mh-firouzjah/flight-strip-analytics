import random
from datetime import date, timedelta

import polars as pl
from dateutil.relativedelta import relativedelta

EXCEL_FILE = "../fsad/data/data.xlsx"
OUTPUT_FILE = "../fsad/data/5months.xlsx"
DATE_COL = "DATE"
DATE_FORMAT = "%d%b"

today = date.today()
start_date = today - relativedelta(months=5)
date_range = [start_date + timedelta(days=i) for i in range((today - start_date).days + 1)]
weights = [(d - start_date).days + 1 for d in date_range]


def random_date_weighted():
    return random.choices(date_range, weights=weights, k=1)[0].strftime(DATE_FORMAT)


def main():
    print("Dispatching random dates with weighted selection...")
    print("Reading Excel file...")
    df = pl.read_excel(EXCEL_FILE)

    print("Generating weighted dates for all rows...")
    num_rows = df.height

    # Generate a list of all dates needed using the new weighted function
    all_dates = [random_date_weighted() for _ in range(num_rows)]

    print("Replacing dates...")
    # Replace the column with the pre-generated list in one efficient operation
    df = df.with_columns(pl.Series(name=DATE_COL, values=all_dates))

    print("Writing to Excel file...")
    df.write_excel(OUTPUT_FILE)
    print("Done!")


if __name__ == "__main__":
    main()
