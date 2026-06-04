# Hermes Google OAuth MCP Server

Small MCP server for Hermes Agent that:
- starts a local FastAPI OAuth flow,
- stores Google OAuth tokens in `google_token.json`,
- provides tools to check token status, schedule Google Meet events, and send emails.

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Create `.env` in project root:
```env
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_client_secret
GOOGLE_REDIRECT_URI=http://localhost:8000/auth/callback
```

3. In Google Cloud Console:
- Enable Gmail API and Google Calendar API.
- Add redirect URI: `http://localhost:8000/auth/callback`

## Add MCP Server To Hermes

Run:
```bash
hermes mcp add my-enterprise-server \
  --command /ABS/PATH/TO/venv/bin/python \
  --args "/ABS/PATH/TO/hermes_oauth_server.py"
```

Example:
```bash
hermes mcp add my-enterprise-server \
  --command /Users/ryaansingh/Desktop/csx/hermes_agent/venv/bin/python \
  --args "/Users/ryaansingh/Desktop/csx/hermes_agent/hermes_oauth_server.py"
```

Then restart Hermes/gateway so new tools are loaded.

## Main MCP Tools

- `start_google_oauth_and_get_login_url`
- `has_access_token`
- `schedule_meet` (creates Calendar event + Google Meet link)
- `send_email`

## Notes

- Do not commit `.env` or `google_token.json`.
- Re-run OAuth when scopes change.
