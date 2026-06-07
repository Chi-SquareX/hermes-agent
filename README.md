# Hermes Google OAuth MCP Server

Small MCP server for Hermes Agent that:
- starts a local FastAPI OAuth flow,
- stores Google OAuth tokens per profile in `google_profiles/`,
- provides tools to check token status, schedule Google Meet events, and send emails from multiple connected accounts.

## WhatsApp + Hermes Gateway (Start Here)

1. Configure WhatsApp in Hermes:
```bash
hermes whatsapp setup
```

2. Verify WhatsApp channel is connected:
```bash
hermes gateway list
```

3. Start Hermes gateway:
```bash
hermes gateway run
```

## Replicate This Repo Locally

Everything sensitive stays on your machine. The repo ships only code and example config shapes.

### 1. Clone and install

```bash
git clone https://github.com/Chi-SquareX/hermes-agent.git
cd hermes-agent
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Create local config from the example

```bash
cp .env.example .env
```

Edit `.env` with your Google Cloud OAuth client ID and secret. Keep `GOOGLE_REDIRECT_URI` aligned with `HERMES_PORT` (default `8010`):

```env
GOOGLE_REDIRECT_URI=http://localhost:8010/auth/callback
HERMES_PORT=8010
```

### 3. Google Cloud Console

- Enable Gmail API and Google Calendar API.
- Create an OAuth client (Desktop or Web).
- Add authorized redirect URI: `http://localhost:8010/auth/callback` (or whatever port you set).

### 4. Connect Google accounts

Start the dashboard (see below), open the login link, and complete OAuth. Tokens are written automatically under `google_profiles/` — you do not copy the example token files by hand.

See `google_profiles/index.json.example` and `google_profiles/token.json.example` for the on-disk layout after OAuth.

## Setup (quick reference)

1. Install dependencies: `pip install -r requirements.txt`
2. Copy `.env.example` → `.env` and fill in credentials.
3. Add the matching redirect URI in Google Cloud Console.

## Add MCP Server To Hermes

Run:
```bash
hermes mcp add my-enterprise-server \
  --command /ABS/PATH/TO/venv/bin/python \
  --args "/ABS/PATH/TO/hermes_oauth_server.py"
```

Example (replace paths with your venv Python and this repo):
```bash
hermes mcp add my-enterprise-server \
  --command /ABS/PATH/TO/.venv/bin/python \
  --args "/ABS/PATH/TO/hermes-agent/hermes_oauth_server.py"
```

Then restart Hermes/gateway so new tools are loaded.

## Add MCP Server To Claude Desktop

1. Open Claude Desktop MCP config file:
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

2. Add this server entry:
```json
{
  "mcpServers": {
    "hermes-oauth": {
      "command": "/path/to/python/interpreter",
      "args": ["/path/to/hermes_oauth_server.py"]
    }
  }
}
```

3. Restart Claude Desktop.

## Email Profiles Dashboard

The dashboard port is controlled by `HERMES_PORT` in `.env` (default **8010**). `GOOGLE_REDIRECT_URI` must use the same port.

### Option A — Docker (recommended)

One container, one port, clean restarts:

```bash
docker compose up --build
```

Stop and free the port:

```bash
docker compose down
```

Open [http://localhost:8010](http://localhost:8010) (or your `HERMES_PORT`).

`google_profiles/`, `expense_data/`, and `.env` are mounted into the container so tokens and expense data persist on your machine.

If the Hermes MCP server is running, it detects the dashboard on the configured port and will not start a duplicate process.

### Option B — Local uvicorn

```bash
python scripts/restart_dashboard.py
```

This stops anything bound to the configured port and starts uvicorn with reload.

Manual start:

```bash
uvicorn app:app --host 127.0.0.1 --port 8010 --reload
```

### Why different ports appeared during development

Multiple stale Python/uvicorn processes were left running (started by the Hermes MCP auto-launcher without reload). New code was started on alternate ports as a workaround when those old processes could not be fully released on Windows. Use one fixed port via `HERMES_PORT` in `.env`, Docker, or `scripts/restart_dashboard.py`.

Each profile uses a `profile_id` (for example `work`, `sales-team-1`). The first account uses `default`; additional accounts get an auto-assigned id from the Google email unless you provide a unique `profile_id` before OAuth.

## Main MCP Tools

- `list_email_profiles` (lists all connected Google accounts)
- `start_google_oauth_and_get_login_url` (optional `profile_id`)
- `has_access_token` (all profiles, or one via `profile_id` / `email`)
- `schedule_meet` (creates Calendar event + Google Meet link)
- `send_email`, `read_emails`, `archive_email` (optional `profile_id` / `email`)

If you have multiple profiles connected, pass `profile_id` or `email` to Gmail/Calendar tools so the agent uses the right account.

## Local-only files (never commit)

These paths are gitignored and stay on your machine only:

| Path | Contents |
|------|----------|
| `.env` | Google OAuth client ID and secret |
| `google_profiles/*.json` | OAuth tokens and profile index (except `*.example`) |
| `expense_data/` | Parsed expense transactions from your email |
| `google_token.json` | Legacy single-account token (auto-migrated) |
| `.oauth_state.json` | Short-lived OAuth CSRF state |

## Notes

- Use `.env.example` and `google_profiles/*.example` as templates — real values are created locally via OAuth.
- Legacy single-account `google_token.json` files are migrated automatically on first use.
- Re-run OAuth when scopes change.
