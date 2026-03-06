#!/usr/bin/env python3
"""
Standalone script to export domain-specific SQLite databases to JSON files.
Scans the directory for .sqlite files, deletes existing JSON exports, and serializes each DB to fresh JSON.
"""

import sqlite3
import json
import os
from pathlib import Path
from crawler.config import DATA_DIR

def export_domain_data():
    """
    Export all domain-specific SQLite databases to JSON files.
    """
    # Find all domain-specific .db files
    db_files = list(DATA_DIR.glob("data_*.db"))

    # Delete existing JSON export files
    for json_file in DATA_DIR.glob("export_*.json"):
        os.remove(json_file)
        print(f"Deleted existing export file: {json_file}")

    # Export each DB to JSON
    for db_file in db_files:
        domain = db_file.stem.replace("data_", "")
        json_file = DATA_DIR / f"export_{domain}.json"

        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM crawl_data")
        rows = cursor.fetchall()

        # Get column names
        column_names = [description[0] for description in cursor.description]

        # Convert to list of dicts
        data = [dict(zip(column_names, row)) for row in rows]

        # Write to JSON
        with open(json_file, 'w') as f:
            json.dump(data, f, indent=4)

        conn.close()
        print(f"Exported {len(data)} records for domain {domain} to {json_file}")

if __name__ == "__main__":
    export_domain_data()
