"""
Airbnb Dublin Listings ETL
Downloads public data, cleans it, creates useful features, and stores the results
in both a SQLite database and CSV files for further analysis in Power BI.
"""

import logging
import sqlite3
from pathlib import Path

import pandas as pd
import numpy as np

# -------------------------------------------------------------------
# Logging setup – we want timestamps and clear levels to monitor the pipeline
# -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# -------------------------------------------------------------------
# Path configuration – everything is relative to this script's location
# so we don't expose any absolute paths when sharing the code.
# -------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"

# Public data source (Inside Airbnb)
DATA_URL = "https://data.insideairbnb.com/ireland/leinster/dublin/2025-09-16/data/listings.csv.gz"

# Local outputs – all stored inside the 'data' folder
DB_PATH = DATA_DIR / "Listings_DB.db"
OUTPUT_CSV = DATA_DIR / "listings_csv.csv"
DROPPED_CSV = DATA_DIR / "dropped_items.csv"

# Create the data folder if it doesn't exist (avoids "directory not found" errors)
DATA_DIR.mkdir(parents=True, exist_ok=True)


def extract_data(url: str) -> pd.DataFrame:
    """
    Download the raw CSV (gzipped) from the given URL and keep only the columns
    we actually need for the analysis. This reduces memory usage and keeps the
    pipeline focused.
    """
    logger.info(f"Downloading data from {url} ...")
    try:
        df = pd.read_csv(url, compression='gzip')
    except Exception as e:
        logger.error(f"Failed to download data: {e}")
        raise

    # These are the columns that are relevant for our business questions
    # (revenue, occupancy, location, host behaviour, etc.)
    cols = [
        "id", "name", "neighbourhood", "neighbourhood_cleansed",
        "room_type", "price", "minimum_nights", "number_of_reviews",
        "reviews_per_month", "availability_365", "latitude", "longitude",
        "property_type", "accommodates", "bedrooms", "host_response_time",
        "host_response_rate", "host_acceptance_rate", "host_is_superhost",
        "estimated_occupancy_l365d", "review_scores_rating"
    ]
    # Keep only columns that actually exist in the dataset (graceful handling
    # if the data source changes slightly)
    existing_cols = [c for c in cols if c in df.columns]
    missing = set(cols) - set(existing_cols)
    if missing:
        logger.warning(f"Columns missing from the dataset: {missing}")
    df = df[existing_cols]
    logger.info(f"Loaded {len(df)} records")
    return df


def clean_and_transform(df: pd.DataFrame):
    """
    Apply all cleaning steps and feature engineering.
    This is where we:
      - remove currency/percentage symbols and convert to numeric
      - drop listings that have zero price, availability, or estimated occupancy
        (these are not meaningful for our revenue/occupancy analysis)
      - create a new sequential ID (since the original IDs are not sequential)
      - bin continuous variables into categories (stay length, occupancy rate)
      - fix string fields and standardise superhost flag
      - compute derived revenue metrics (max possible revenue, estimated revenue)
    """
    logger.info("Starting cleaning and transformation...")
    df = df.copy()

    # 1. Strip '$' and '%' from price, response rate, acceptance rate, then coerce to numeric
    #    We do this before any other numeric conversion to avoid errors.
    cols_to_clean = ['price', 'host_response_rate', 'host_acceptance_rate']
    for col in cols_to_clean:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(r'[\$,\%]', '', regex=True)

    numeric_cols = [
        'price', 'host_response_rate', 'host_acceptance_rate',
        'availability_365', 'minimum_nights', 'number_of_reviews',
        'reviews_per_month', 'accommodates', 'bedrooms',
        'estimated_occupancy_l365d', 'review_scores_rating',
        'latitude', 'longitude'
    ]
    for col in numeric_cols:
        if col in df.columns:
            # Convert to numeric; if conversion fails, set to 0.0 (we'll filter later)
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0.0)

    # 2. Identify listings that should be dropped because they have zero (or negative)
    #    price, availability, or estimated occupancy. These are either placeholders
    #    or inactive listings, and including them would skew the revenue metrics.
    mask_invalid = (
        (df['price'] <= 0) |
        (df['availability_365'] <= 0) |
        (df['estimated_occupancy_l365d'] <= 0)
    )
    dropped = df[mask_invalid].copy()

    # Build a clear reason for each dropped record – useful for auditing later.
    reasons = []
    for _, row in dropped.iterrows():
        r = []
        if row['price'] <= 0:
            r.append("Price equal zero")
        if row['availability_365'] <= 0:
            r.append("Availability equal zero")
        if row['estimated_occupancy_l365d'] <= 0:
            r.append("Estimated occupancy equal zero")
        reasons.append("; ".join(r) if r else "Multiple reasons")
    dropped['reason_for_dropping'] = reasons

    # Keep only the valid listings
    df_clean = df[~mask_invalid].copy()

    # 3. Generate a new sequential ID (as string) to have a clean primary key
    #    that doesn't have gaps and is independent of the original Airbnb ID.
    df_clean = df_clean.reset_index(drop=True)
    df_clean['id'] = (df_clean.index + 1).astype(str)

    # 4. Convert certain columns to integers – they represent counts or nights,
    #    so decimals don't make sense after cleaning.
    int_cols = ["minimum_nights", "number_of_reviews", "availability_365",
                "accommodates", "estimated_occupancy_l365d"]
    for col in int_cols:
        if col in df_clean.columns:
            df_clean[col] = df_clean[col].astype(int)

    # 5. Create a categorical variable for stay length (minimum nights).
    #    This helps group properties by their booking commitment level.
    bins_nights = [0, 2, 6, 29, 9999]
    labels_nights = ['1-2 nights', '3-6 nights', '7-29 nights', '30+ nights']
    df_clean['stay_category'] = pd.cut(
        df_clean['minimum_nights'],
        bins=bins_nights,
        labels=labels_nights,
        include_lowest=True
    )

    # 6. Create occupancy clusters based on estimated occupancy per year.
    #    The bins correspond to quartiles of the 365-day year (25%, 50%, 75%).
    bins_occ = [0, 91.25, 182.5, 273.75, 365]
    labels_occ = ['0-25%', '25-50%', '50-75%', '75-100%']
    df_clean['occupancy_cluster'] = pd.cut(
        df_clean['estimated_occupancy_l365d'],
        bins=bins_occ,
        labels=labels_occ,
        include_lowest=True
    )

    # 7. Build a location string for mapping in Power BI, and standardise the
    #    superhost flag to 'Yes'/'No' (instead of 't'/'f').
    df_clean['location'] = df_clean['latitude'].astype(str) + ", " + df_clean['longitude'].astype(str)
    df_clean['host_is_superhost'] = df_clean['host_is_superhost'].fillna('f').str.upper()
    df_clean['host_is_superhost'] = df_clean['host_is_superhost'].replace({'F': 'No', 'T': 'Yes'})

    # 8. Strip whitespace from all text columns to avoid inconsistent matching later.
    str_cols = [
        "name", "neighbourhood", "neighbourhood_cleansed", "room_type",
        "property_type", "location", "stay_category", "occupancy_cluster",
        "host_response_time"
    ]
    for col in str_cols:
        if col in df_clean.columns:
            df_clean[col] = df_clean[col].astype(str).str.strip()

    # One specific correction: the original dataset uses a shorthand for
    # "Dún Laoghaire-Rathdown" – we fix it to the standard spelling.
    df_clean['neighbourhood_cleansed'] = df_clean['neighbourhood_cleansed'].replace(
        'Dn Laoghaire-Rathdown', 'Dun Laoghaire-Rathdown'
    )

    # 9. Calculate revenue estimates:
    #    - max_revenue: what they could earn if booked every day of the year
    #    - estimated_revenue: more realistic, based on estimated occupancy
    df_clean['max_revenue'] = df_clean["availability_365"] * df_clean["price"]
    df_clean['estimated_revenue'] = df_clean["estimated_occupancy_l365d"] * df_clean["price"]

    logger.info(f"Kept {len(df_clean)} records, dropped {len(dropped)}")
    return df_clean, dropped


def load_to_database(df: pd.DataFrame, dropped: pd.DataFrame, db_path: Path):
    """
    Write the cleaned data and the dropped items log to a SQLite database.
    The schema for the main table is defined to keep numeric types correct
    (especially for columns we'll use in calculations).
    """
    logger.info(f"Loading data to database at {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        # Explicitly set the data types for key columns – this helps when querying
        # later and avoids SQLite's automatic type inference issues.
        db_schema = {
            "id": "TEXT",
            "price": "REAL",
            "minimum_nights": "INTEGER",
            "availability_365": "INTEGER",
            "estimated_occupancy_l365d": "INTEGER",
            "occupancy_cluster": "TEXT",
            "max_revenue": "REAL",
            "estimated_revenue": "REAL"
        }
        # Save the main listings and the dropped log as separate tables.
        df.to_sql("listings", conn, if_exists="replace", index=False, dtype=db_schema)
        dropped.to_sql("dropped_listings", conn, if_exists="replace", index=False)
        logger.info("Database load successful.")
    except Exception as e:
        logger.error(f"Failed to load data into database: {e}")
        raise
    finally:
        conn.close()


def save_csvs(df: pd.DataFrame, dropped: pd.DataFrame, output_csv: Path, dropped_csv: Path):
    """
    Export the cleaned DataFrame and the dropped items log to CSV files.
    These are used as a backup and also for manual inspection if needed.
    """
    logger.info("Exporting CSVs...")
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    dropped_csv.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(output_csv, index=False, encoding='utf-8-sig')
    dropped.to_csv(dropped_csv, index=False, encoding='utf-8-sig')
    logger.info(f"Main CSV saved to {output_csv}")
    logger.info(f"Dropped items CSV saved to {dropped_csv}")


def main():
    """Orchestrate the full ETL pipeline: extract → transform → load → export."""
    try:
        raw_df = extract_data(DATA_URL)
        clean_df, dropped_df = clean_and_transform(raw_df)
        load_to_database(clean_df, dropped_df, DB_PATH)
        save_csvs(clean_df, dropped_df, OUTPUT_CSV, DROPPED_CSV)

        logger.info("-" * 30)
        logger.info("ETL Process completed successfully!")
        logger.info("-" * 30)

    except Exception as e:
        logger.error(f"ETL pipeline failed: {e}")
        raise


if __name__ == "__main__":
    main()