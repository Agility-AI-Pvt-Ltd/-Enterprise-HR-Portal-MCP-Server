"""
NDDB AI4IT — Day 5 Enterprise HR Portal MCP Server
=============================================================================
Model Context Protocol (MCP) server backed by Neon PostgreSQL.
Exposes HR & self-service tools to any MCP client (Claude Desktop, Cursor,
Continue.dev, or custom AI agents).

Transports
  * stdio            : local use (Claude Desktop config file)      MCP_TRANSPORT=stdio
  * streamable-http  : remote use (Render → Claude custom connector) MCP_TRANSPORT=http
    Endpoint: https://<your-app>.onrender.com/mcp     Health: /health

Configuration (environment variables / .env) — see .env.example
  NEON_DATABASE_URL  (required) Neon connection string
  MCP_API_KEY        (optional) if set, every /mcp request must send it as
                     `Authorization: Bearer <key>` or `?api_key=<key>`
  MCP_TRANSPORT      stdio | http   (default: http when PORT is set, else stdio)
  PORT / HOST        HTTP bind (Render sets PORT automatically)

Tables: employees, leave_balances, leave_applications, agent_audit_logs

Tools
  1. register_employee    : Onboard an employee and allocate leaves.
  2. get_remaining_leaves : Live leave balance + recent applications.
  3. apply_for_leave      : Validate balance, deduct days, log application.
  4. list_all_employees   : Roster with remaining leaves.
"""

import os
import re
import sys
import time
import uuid
import hmac
import logging
from contextlib import contextmanager
from datetime import date

import psycopg2
from psycopg2 import errors as pg_errors
from psycopg2.extras import RealDictCursor

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: the 'mcp' package is required. Install via: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

# Logs go to stderr so they never corrupt the stdio JSON-RPC stream.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("nddb-hr-portal")

# =============================================================================
# CONFIGURATION
# =============================================================================
AGENT_NAME = "nddb-hr-portal-mcp"
LEAVE_POLICY = {"casual": 12, "sick": 10, "earned": 18}  # days per year
ALLOWED_LEAVE_TYPES = tuple(LEAVE_POLICY)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_db_url(url: str) -> str:
    """psycopg2 builds against older libpq that may reject channel_binding; strip it."""
    url = url.strip().strip('"').strip("'")
    url = re.sub(r"[?&]channel_binding=[^&]*", "", url)
    if "?" not in url and "&" in url:
        url = url.replace("&", "?", 1)
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


_raw_url = os.environ.get("NEON_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not _raw_url:
    log.error("NEON_DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    sys.exit(1)
DATABASE_URL = _clean_db_url(_raw_url)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
TRANSPORT = os.environ.get("MCP_TRANSPORT", "http" if os.environ.get("PORT") else "stdio").lower()
API_KEY = os.environ.get("MCP_API_KEY", "").strip()

app = FastMCP(
    "nddb-hr-portal",
    instructions="HR self-service for NDDB: register employees, check leave balances, apply for leave, list employees.",
    host=HOST,
    port=PORT,
    stateless_http=True,  # no sticky sessions needed; survives Render restarts
    json_response=True,
)

# =============================================================================
# DATABASE HELPERS
# =============================================================================


def _connect(retries: int = 3):
    """Connect with retry/backoff — Neon computes auto-suspend and the first
    connection after idle can fail or be slow while the compute wakes up."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg2.connect(
                DATABASE_URL,
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                application_name=AGENT_NAME,
            )
        except psycopg2.OperationalError as e:
            last_err = e
            log.warning("DB connect attempt %d/%d failed: %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise last_err


@contextmanager
def db_cursor(dict_rows: bool = False):
    """Yields a cursor inside a transaction. Commits on success, rolls back on
    any error, and ALWAYS closes the connection (no leaks)."""
    conn = _connect()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor if dict_rows else None)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def log_audit(cursor, action: str, details: str, status: str = "SUCCESS"):
    """Best-effort audit trail. Uses a SAVEPOINT so a failure here cannot abort
    the surrounding transaction (in PostgreSQL one failed statement poisons the
    whole transaction, which would silently roll back the real work)."""
    try:
        cursor.execute("SAVEPOINT audit_sp")
        cursor.execute(
            """INSERT INTO agent_audit_logs (agent_name, action_performed, details, status)
               VALUES (%s, %s, %s, %s)""",
            (AGENT_NAME, action, details, status),
        )
        cursor.execute("RELEASE SAVEPOINT audit_sp")
    except Exception as e:
        cursor.execute("ROLLBACK TO SAVEPOINT audit_sp")
        log.warning("Audit log skipped: %s", e)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS employees (
    employee_id   VARCHAR(50) PRIMARY KEY,
    full_name     VARCHAR(200) NOT NULL,
    email         VARCHAR(255) UNIQUE,
    department    VARCHAR(150),
    designation   VARCHAR(150),
    joining_date  DATE DEFAULT CURRENT_DATE,
    status        VARCHAR(20) DEFAULT 'ACTIVE',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS leave_balances (
    id              SERIAL PRIMARY KEY,
    employee_id     VARCHAR(50) REFERENCES employees(employee_id) ON DELETE CASCADE,
    leave_type      VARCHAR(30) NOT NULL,
    total_allocated INTEGER NOT NULL DEFAULT 0,
    used_days       INTEGER NOT NULL DEFAULT 0,
    remaining_days  INTEGER GENERATED ALWAYS AS (total_allocated - used_days) STORED,
    year            INTEGER NOT NULL,
    last_updated    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (employee_id, leave_type, year)
);
CREATE TABLE IF NOT EXISTS leave_applications (
    application_id VARCHAR(50) PRIMARY KEY,
    employee_id    VARCHAR(50) REFERENCES employees(employee_id) ON DELETE CASCADE,
    leave_type     VARCHAR(30) NOT NULL,
    start_date     DATE,
    end_date       DATE,
    days_count     INTEGER NOT NULL,
    reason         TEXT,
    status         VARCHAR(20) DEFAULT 'PENDING',
    applied_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS agent_audit_logs (
    id               SERIAL PRIMARY KEY,
    agent_name       VARCHAR(100),
    action_performed VARCHAR(100),
    details          TEXT,
    status           VARCHAR(20),
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def ensure_schema():
    """Create tables if they don't exist (no-op on an existing database).
    Never fatal: the server still starts if the DB is briefly unreachable."""
    if os.environ.get("SKIP_SCHEMA_INIT", "").lower() in ("1", "true", "yes"):
        return
    try:
        with db_cursor() as cur:
            cur.execute(SCHEMA_SQL)
        log.info("Database schema verified.")
    except Exception as e:
        log.warning("Schema check skipped (DB unreachable or insufficient rights): %s", e)


def _ensure_balances(cur, emp_id: str, year: int):
    """Fallback: create this year's allocation if missing (e.g. new year rollover)."""
    cur.executemany(
        """INSERT INTO leave_balances (employee_id, leave_type, total_allocated, used_days, year)
           VALUES (%s, %s, %s, 0, %s)
           ON CONFLICT (employee_id, leave_type, year) DO NOTHING""",
        [(emp_id, lt, days, year) for lt, days in LEAVE_POLICY.items()],
    )


def _db_error(action: str, e: Exception) -> str:
    log.exception("DB error during %s", action)
    if isinstance(e, psycopg2.OperationalError):
        return f"Database is temporarily unavailable during {action}. Please retry in a few seconds."
    return f"Database error during {action}: {str(e).strip().splitlines()[0]}"


# =============================================================================
# MCP TOOLS
# =============================================================================


@app.tool()
def register_employee(employee_id: str, full_name: str, email: str, department: str, designation: str) -> str:
    """Register a new employee in the NDDB enterprise HR system.

    Args:
        employee_id: Unique Employee ID (e.g., 'EMP-882' or 'NDDB-2026-05').
        full_name: Full legal name of the employee/attendee.
        email: Official enterprise email address.
        department: Assigned department (e.g., 'ICT Infrastructure', 'Cold-Chain Engineering', 'Procurement').
        designation: Job title / role (e.g., 'Senior Systems Engineer', 'DBA Specialist').

    Returns:
        Confirmation with initial leave allocations (12 Casual, 10 Sick, 18 Earned).
    """
    emp_id = (employee_id or "").strip().upper()
    name = (full_name or "").strip()
    email_clean = (email or "").strip().lower()
    dept = (department or "").strip()
    desig = (designation or "").strip()
    year = date.today().year

    if not emp_id or not name:
        return "Registration Error: employee_id and full_name are required."
    if len(emp_id) > 50:
        return "Registration Error: employee_id must be at most 50 characters."
    if email_clean and not EMAIL_RE.match(email_clean):
        return f"Registration Error: '{email}' is not a valid email address."

    try:
        with db_cursor() as cur:
            cur.execute("SELECT full_name FROM employees WHERE employee_id = %s", (emp_id,))
            existing = cur.fetchone()
            if existing:
                return f"Registration Error: Employee ID '{emp_id}' is already registered to '{existing[0]}'."

            cur.execute(
                """INSERT INTO employees (employee_id, full_name, email, department, designation, joining_date, status)
                   VALUES (%s, %s, %s, %s, %s, CURRENT_DATE, 'ACTIVE')""",
                (emp_id, name, email_clean or None, dept, desig),
            )
            _ensure_balances(cur, emp_id, year)
            log_audit(cur, "register_employee", f"Onboarded {name} ({emp_id})")
    except pg_errors.UniqueViolation:
        return f"Registration Error: an employee with ID '{emp_id}' or email '{email_clean}' already exists."
    except Exception as e:
        return _db_error("registration", e)

    total = sum(LEAVE_POLICY.values())
    return (
        f"✅ Employee Registration Successful!\n"
        f"• Employee ID  : {emp_id}\n"
        f"• Full Name    : {name}\n"
        f"• Department   : {dept}\n"
        f"• Designation  : {desig}\n"
        f"• Email        : {email_clean or 'N/A'}\n"
        f"• Allocated Leaves: {LEAVE_POLICY['casual']} Casual, {LEAVE_POLICY['sick']} Sick, "
        f"{LEAVE_POLICY['earned']} Earned (Total: {total} days for {year})"
    )


@app.tool()
def get_remaining_leaves(employee_id: str) -> str:
    """Check remaining leave balances and recent application history.

    Args:
        employee_id: The Employee ID to query (e.g., 'EMP-101').

    Returns:
        Leave balance breakdown and the 3 most recent applications.
    """
    emp_id = (employee_id or "").strip().upper()
    year = date.today().year
    if not emp_id:
        return "Error: employee_id is required."

    try:
        with db_cursor(dict_rows=True) as cur:
            cur.execute(
                "SELECT full_name, department, designation, status FROM employees WHERE employee_id = %s",
                (emp_id,),
            )
            emp = cur.fetchone()
            if not emp:
                return f"Error: No employee found with ID '{emp_id}'. Use 'register_employee' to create a profile."

            _ensure_balances(cur, emp_id, year)
            # remaining computed here so it works whether or not the column is generated
            cur.execute(
                """SELECT leave_type, total_allocated, used_days,
                          (total_allocated - used_days) AS remaining_days
                   FROM leave_balances
                   WHERE employee_id = %s AND year = %s
                   ORDER BY leave_type""",
                (emp_id, year),
            )
            balances = cur.fetchall()

            cur.execute(
                """SELECT application_id, leave_type, days_count, status, reason, applied_at
                   FROM leave_applications
                   WHERE employee_id = %s
                   ORDER BY applied_at DESC NULLS LAST
                   LIMIT 3""",
                (emp_id,),
            )
            recent = cur.fetchall()
    except Exception as e:
        return _db_error("leave lookup", e)

    balance_lines, total_remaining = [], 0
    for b in balances:
        rem = b["remaining_days"] or 0
        total_remaining += rem
        balance_lines.append(
            f"  • {b['leave_type'].capitalize():<10} : {rem:>2} remaining "
            f"(Allocated: {b['total_allocated']}, Used: {b['used_days']})"
        )
    if not balance_lines:
        balance_lines.append("  • No balance records found.")

    history = []
    for r in recent:
        applied = r["applied_at"].strftime("%Y-%m-%d") if r["applied_at"] else "N/A"
        history.append(
            f"  - [{applied}] {r['days_count']}d {(r['leave_type'] or '').capitalize()} "
            f"({r['status']}) - Reason: {r['reason'] or '—'}"
        )
    if not history:
        history.append("  - No prior leave applications recorded.")

    return (
        f"📋 NDDB Leave Balance Report:\n"
        f"• Employee   : {emp['full_name']} ({emp_id})\n"
        f"• Department : {emp['department'] or '—'} | {emp['designation'] or '—'}\n"
        f"• Status     : {emp['status']}\n\n"
        f"📊 Leave Balances (Year {year}):\n" + "\n".join(balance_lines) + "\n"
        f"  ------------------------------------------------\n"
        f"  • Total Available : {total_remaining} days\n\n"
        f"📝 Recent Applications:\n" + "\n".join(history)
    )


@app.tool()
def apply_for_leave(employee_id: str, leave_type: str, days_count: int, reason: str, start_date: str = "") -> str:
    """Apply for leave: validates the balance, deducts days, and records the application.

    Args:
        employee_id: Employee ID applying for leave (e.g., 'EMP-101').
        leave_type: 'casual', 'sick', or 'earned'.
        days_count: Number of days requested (positive integer, max 60).
        reason: Justification / purpose for the leave.
        start_date: Optional start date in YYYY-MM-DD format (defaults to today).

    Returns:
        Approval confirmation with updated balance, or rejection details.
    """
    emp_id = (employee_id or "").strip().upper()
    l_type = (leave_type or "").strip().lower()
    reason_clean = (reason or "").strip()
    year = date.today().year

    try:
        days = int(days_count)
    except (TypeError, ValueError):
        return "Application Error: days_count must be a whole number."
    if days <= 0:
        return "Application Error: Number of days requested must be at least 1."
    if days > 60:
        return "Application Error: A single application cannot exceed 60 days."
    if l_type not in ALLOWED_LEAVE_TYPES:
        return f"Application Error: Invalid leave type '{leave_type}'. Allowed: {', '.join(ALLOWED_LEAVE_TYPES)}."
    if not reason_clean:
        return "Application Error: A reason for the leave is required."

    try:
        start = date.fromisoformat(start_date.strip()) if start_date and start_date.strip() else date.today()
    except ValueError:
        return f"Application Error: start_date '{start_date}' must be in YYYY-MM-DD format."
    end = date.fromordinal(start.toordinal() + days - 1)

    try:
        with db_cursor(dict_rows=True) as cur:
            cur.execute("SELECT full_name, status FROM employees WHERE employee_id = %s", (emp_id,))
            emp = cur.fetchone()
            if not emp:
                return f"Application Error: Employee '{emp_id}' not found. Please register first."
            if emp["status"] and emp["status"].upper() != "ACTIVE":
                return f"Application Error: Employee '{emp_id}' is {emp['status']} and cannot apply for leave."

            _ensure_balances(cur, emp_id, year)
            # FOR UPDATE locks the row so two simultaneous requests can't overspend
            cur.execute(
                """SELECT total_allocated, used_days, (total_allocated - used_days) AS remaining_days
                   FROM leave_balances
                   WHERE employee_id = %s AND leave_type = %s AND year = %s
                   FOR UPDATE""",
                (emp_id, l_type, year),
            )
            bal = cur.fetchone()
            if not bal:
                return f"Application Error: No {l_type} leave balance found for {year}."

            rem = bal["remaining_days"]
            if rem < days:
                log_audit(cur, "apply_for_leave", f"{emp_id} rejected: {days}d {l_type}, {rem} left", "REJECTED")
                return (
                    f"❌ Leave Application REJECTED for {emp['full_name']} ({emp_id}):\n"
                    f"• Reason     : Insufficient {l_type.capitalize()} Leave balance.\n"
                    f"• Requested  : {days} days\n"
                    f"• Available  : {rem} days (Shortfall: {days - rem} days)"
                )

            cur.execute(
                """UPDATE leave_balances
                   SET used_days = used_days + %s, last_updated = CURRENT_TIMESTAMP
                   WHERE employee_id = %s AND leave_type = %s AND year = %s""",
                (days, emp_id, l_type, year),
            )
            app_id = f"APP-{uuid.uuid4().hex[:8].upper()}"
            cur.execute(
                """INSERT INTO leave_applications
                       (application_id, employee_id, leave_type, start_date, end_date, days_count, reason, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'APPROVED')""",
                (app_id, emp_id, l_type, start, end, days, reason_clean),
            )
            log_audit(cur, "apply_for_leave", f"{emp['full_name']} applied {days}d {l_type} ({app_id})")
    except Exception as e:
        return _db_error("leave application", e)

    return (
        f"✅ Leave Application APPROVED!\n"
        f"• Application ID : {app_id}\n"
        f"• Employee       : {emp['full_name']} ({emp_id})\n"
        f"• Leave Type     : {l_type.capitalize()} Leave\n"
        f"• Dates          : {start.isoformat()} → {end.isoformat()} ({days} day(s))\n"
        f"• Reason         : {reason_clean}\n"
        f"• Updated Balance: {rem - days} days remaining (was {rem})\n"
        f"• Status         : APPROVED"
    )


@app.tool()
def list_all_employees() -> str:
    """List all registered employees with their total remaining leaves for the current year."""
    year = date.today().year
    try:
        with db_cursor(dict_rows=True) as cur:
            cur.execute(
                """SELECT e.employee_id, e.full_name, e.department, e.designation,
                          COALESCE(SUM(b.total_allocated - b.used_days), 0) AS total_remaining_leaves
                   FROM employees e
                   LEFT JOIN leave_balances b ON e.employee_id = b.employee_id AND b.year = %s
                   GROUP BY e.employee_id, e.full_name, e.department, e.designation
                   ORDER BY e.employee_id""",
                (year,),
            )
            rows = cur.fetchall()
    except Exception as e:
        return _db_error("employee listing", e)

    if not rows:
        return "No employees registered yet."

    def cut(v, n):
        v = str(v or "—")
        return v if len(v) <= n else v[: n - 1] + "…"

    lines = [f"{'ID':<14} | {'Name':<22} | {'Department':<26} | {'Designation':<22} | Leaves Left"]
    lines.append("-" * 104)
    for r in rows:
        lines.append(
            f"{cut(r['employee_id'], 14):<14} | {cut(r['full_name'], 22):<22} | {cut(r['department'], 26):<26} | "
            f"{cut(r['designation'], 22):<22} | {r['total_remaining_leaves']}"
        )
    return (
        "👥 Registered NDDB Employees:\n" + "\n".join(lines) + f"\n\nTotal Registered Employees: {len(rows)}"
    )


# =============================================================================
# HTTP APP (Render) — health check + optional API-key protection
# =============================================================================


def build_http_app():
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse

    http_app = app.streamable_http_app()

    async def health(request):
        return JSONResponse({"status": "ok", "server": "nddb-hr-portal", "mcp_endpoint": "/mcp"})

    async def root(request):
        return PlainTextResponse("NDDB HR Portal MCP server. Connect your MCP client to /mcp\n")

    http_app.add_route("/health", health, methods=["GET", "HEAD"])
    http_app.add_route("/", root, methods=["GET", "HEAD"])

    if API_KEY:

        class ApiKeyMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if request.url.path.startswith("/mcp"):
                    auth = request.headers.get("authorization", "")
                    supplied = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                    supplied = supplied or request.query_params.get("api_key", "")
                    if not hmac.compare_digest(supplied, API_KEY):
                        return JSONResponse({"error": "unauthorized"}, status_code=401)
                return await call_next(request)

        http_app.add_middleware(ApiKeyMiddleware)
        log.info("API key protection enabled for /mcp")
    else:
        log.warning("MCP_API_KEY not set — /mcp is publicly accessible.")

    return http_app


# =============================================================================
# ENTRYPOINT
# =============================================================================
if __name__ == "__main__":
    ensure_schema()
    if TRANSPORT in ("http", "streamable-http", "streamable_http"):
        import uvicorn

        log.info("Starting NDDB HR Portal MCP server (HTTP) on %s:%d — endpoint /mcp", HOST, PORT)
        uvicorn.run(build_http_app(), host=HOST, port=PORT, proxy_headers=True, forwarded_allow_ips="*")
    else:
        log.info("Starting NDDB HR Portal MCP server (stdio)")
        app.run()
