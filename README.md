# NDDB Enterprise HR Portal — MCP Server

An MCP server backed by Neon PostgreSQL that provides HR self-service tools to Claude Desktop, Cursor, or any other MCP client.

| Tool | Purpose |
|---|---|
| `register_employee` | Onboard an employee and allocate 12 casual, 10 sick and 18 earned leave days |
| `get_remaining_leaves` | Show live balances and the 3 most recent applications |
| `apply_for_leave` | Check the balance, deduct the days and record the application (row-locked) |
| `list_all_employees` | Show the roster with remaining leave |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill NEON_DATABASE_URL
python server.py              # stdio mode (default without PORT)
MCP_TRANSPORT=http python server.py   # HTTP on http://localhost:8000/mcp
```

On first start, missing tables are created automatically. Existing tables are left as they are. To skip this step, set `SKIP_SCHEMA_INIT=1`.

## Deploy on Render

1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint** and pick the repo. It reads `render.yaml`.
3. When Render asks, enter `NEON_DATABASE_URL` and `SERVER_PASSWORD`.
4. Once the deploy finishes, `https://<service>.onrender.com/health` should return `{"status":"ok"}`.

The MCP endpoint is `https://<service>.onrender.com/mcp`.

> On Render's free plan, the service sleeps after 15 minutes without traffic, so the first call after a break can take about 30–60 s. Neon also suspends its compute when idle. The server retries the database connection to cover both.

## Connect Claude Desktop

**Remote (Render):**

1. In Claude, go to **Settings → Connectors → Add custom connector**.
2. Enter the URL `https://<service>.onrender.com/mcp`.
3. Click **Connect**. A login page opens in your browser.
4. Enter `SERVER_PASSWORD`. You're connected.

After that, the login lasts 30 days, and the connection stays active across server restarts. **Changing `SERVER_PASSWORD` in Render disconnects everyone**, and each client has to log in again.

**Other clients** (Cursor, MCP Inspector, scripts) can skip the login page and send the password as a header:

```
Authorization: Bearer <SERVER_PASSWORD>
```

**Local (stdio):** Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "nddb-hr-portal": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/server.py"],
      "env": { "NEON_DATABASE_URL": "postgresql://...", "MCP_TRANSPORT": "stdio" }
    }
  }
}
```

## Environment variables

| Var | Required | Notes |
|---|---|---|
| `NEON_DATABASE_URL` | yes | `channel_binding` is removed automatically, and `sslmode=require` is added if it's missing |
| `SERVER_PASSWORD` | yes (HTTP) | The shared password. The server won't start in HTTP mode without it. After 5 wrong attempts, an IP address is blocked for 10 minutes |
| `PUBLIC_URL` | no | The public base URL used in the OAuth login flow. On Render it's detected automatically from `RENDER_EXTERNAL_URL` |
| `MCP_TRANSPORT` | no | `stdio` or `http`. Defaults to `http` when `PORT` is set |
| `PORT` / `HOST` | no | Render sets `PORT` for you |
