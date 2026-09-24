"""Check that the Neon HR database is reachable and the tables have data.

Usage:
    cp .env.example .env   # set NEON_DATABASE_URL
    python test_db.py

Exit code 0 when every expected table exists and has at least one row.
"""

import os
import sys
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

EXPECTED_HOST = "ep-wandering-water-b52fnnnb-pooler.c-7.us-east-2.aws.neon.tech"
EXPECTED_DB = "neondb"
TABLES = (
    "employees",
    "leave_balances",
    "leave_applications",
    "agent_audit_logs",
)


def _clean_db_url(url: str) -> str:
    url = url.strip().strip('"').strip("'")
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _connect():
    raw = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        print("FAIL  NEON_DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    parsed = urlparse(raw.strip().strip('"').strip("'"))
    host = (parsed.hostname or "").lower()
    dbname = (parsed.path or "").lstrip("/")
    if host != EXPECTED_HOST:
        print(f"FAIL  host is '{host}', expected '{EXPECTED_HOST}'")
        sys.exit(1)
    if dbname != EXPECTED_DB:
        print(f"FAIL  database is '{dbname}', expected '{EXPECTED_DB}'")
        sys.exit(1)

    return psycopg2.connect(_clean_db_url(raw), connect_timeout=15, application_name="nddb-hr-db-test")


def main() -> int:
    failures = []
    print(f"Connecting to {EXPECTED_HOST} / {EXPECTED_DB} ...")
    try:
        conn = _connect()
    except Exception as e:
        print(f"FAIL  connection: {e}")
        return 1

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT current_database() AS db, current_user AS user")
            info = cur.fetchone()
            print(f"OK    connected as {info['user']} to database {info['db']}")

            for table in TABLES:
                cur.execute(
                    """SELECT COUNT(*) AS n
                       FROM information_schema.tables
                       WHERE table_schema = 'public' AND table_name = %s""",
                    (table,),
                )
                if cur.fetchone()["n"] == 0:
                    print(f"FAIL  {table}: table does not exist")
                    failures.append(table)
                    continue

                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                count = cur.fetchone()["n"]
                if count == 0:
                    print(f"FAIL  {table}: exists but has 0 rows")
                    failures.append(table)
                    continue

                cur.execute(f"SELECT * FROM {table} LIMIT 1")
                sample = cur.fetchone()
                columns = ", ".join(sample.keys())
                print(f"OK    {table}: {count} row(s)  columns: {columns}")
    finally:
        conn.close()

    print()
    if failures:
        print(f"FAILED  {len(failures)} table(s): {', '.join(failures)}")
        return 1
    print(f"PASSED  all {len(TABLES)} tables are reachable and contain data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
