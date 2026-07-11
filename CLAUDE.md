# Hermes Agent Railway Template

## Architecture

Python/Starlette web server that wraps Hermes Agent's gateway as a managed subprocess.

- `server.py` — Main server: HTTP handlers, gateway process manager, basic auth, .env file management
- `templates/index.html` — Single-page UI with Tailwind CSS + Alpine.js
- Config is stored as a flat `.env` file at `/data/.hermes/.env` (Hermes uses python-dotenv)
- Gateway is spawned via `hermes gateway` command with env vars from the .env file

## Key patterns

- Gateway lifecycle: start/stop/restart via async subprocess, stdout captured to ring buffer
- Secret masking: password fields show first 8 chars + `***`, merge on save preserves masked values
- No direct Hermes Python imports — the server manages the .env file independently
- Auto-start: gateway starts on server boot when a messaging channel is configured, unless `GATEWAY_AUTO_START` explicitly overrides it
- Production lifecycle: the server supervises `hermes gateway run`, immediately honors Hermes exit 75 restarts, and backs off unexpected crash restarts
