# Development Notes

This project runs a Telegram bot that forwards user messages to a resident Codex agent via `codex app-server` (stdio JSON-RPC).

## Quick Checks

- Codex is available: `codex --version`
- App-server protocol works: `python codex_app_server_probe.py --prompt ping`

## Local Run

```bash
./scripts/install.sh
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_ALLOWED_USER_IDS=5974809970
./scripts/run.sh
```

## Security

- Always set `TELEGRAM_ALLOWED_USER_IDS` for a PC-controlling bot.
- Do not commit `codex_config.json` (contains the token). It is already in `.gitignore`.

