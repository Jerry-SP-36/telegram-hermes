# Telegram Response Bridge

This service owns the response-rendering boundary for Hermes. Hermes keeps its
existing Telegram adapter, polling, media handling, sessions, and tools. Its
Telegram sender calls the bridge's private `/render` endpoint before using its
existing sender, so only outgoing response text is transformed:

```text
Telegram -> Hermes -> JSON response -> Response Bridge /render -> Telegram sender -> Telegram
```

Every `sendMessage` text must be a Hermes response JSON object.  The bridge
extracts the one JSON object even when a model incorrectly wraps it in a code
fence or short preamble, normalizes the safe legacy subset containing only
`type` and `summary` into the full fixed contract, validates it, maps it to a
short Telegram message, and never forwards the JSON or a malformed free-text
response to the user.

## Why this boundary

The bridge retains a restricted Bot API proxy route as a compatibility fallback
for Hermes versions that honour a custom Telegram `base_url`. The primary path
uses `/render`, which preserves the working inbound path and changes only the
last Telegram-sender step.

The bridge has no database, queue, state machine, callback routing, or
confidence-based routing.

## Local verification

```sh
python3 -m unittest discover -s tests -v
```

## Zeabur deployment

1. Create a new service from this directory's Dockerfile.  No Telegram token
   variable is required: Hermes already includes its token in each Bot API
   request path, and the bridge only accepts private-network traffic.  Do not
   put the token in source code or logs.  If a deployment policy requires an
   additional path check, set `TELEGRAM_BOT_TOKEN` to the existing token; this
   is optional and must never be committed.
2. Obtain the bridge service's private hostname from Zeabur's Networking page.
   It will be similar to `telegram-response-bridge.zeabur.internal`.
3. Install [`hermes/telegram_response_sender.py`](hermes/telegram_response_sender.py)
   as `sitecustomize.py` in the Hermes runtime Python path, changing
   `RENDER_URL` to the exact private hostname.  Configure Hermes to load that
   directory through `PYTHONPATH`.  The hook changes only replies that point
   back to an incoming Telegram message; startup notices and inbound polling
   remain untouched.
4. Append the contents of
   [`hermes/AGENTS-telegram-json.md`](hermes/AGENTS-telegram-json.md) to the
   active Hermes workspace's existing `AGENTS.md`.  Do not replace unrelated
   workspace instructions.
5. Restart Hermes once.  There must be only one Hermes Telegram poller for the
   bot token.  The bridge does not poll Telegram itself.
6. Test `success`, `question`, and `error` in a private Telegram chat.  A
   single reply must traverse the sender hook exactly once; do not patch both
   `Bot.send_message` and `ExtBot.send_message`.

## Health check

`GET /health` returns `{"status":"ok"}` and does not contain configuration
or secrets.

## Security choices

- The bridge accepts only well-formed Telegram Bot API paths and forwards them
  only to `https://api.telegram.org` (or an explicitly configured test origin).
  It runs on Zeabur private networking and does not store the bot token.  An
  optional `TELEGRAM_BOT_TOKEN` pins accepted paths to one known token.
- Uvicorn access logging is disabled so request URLs cannot reveal the bot
  token.
- A `sendMessage` with free text, malformed JSON, unknown fields, or an
  invalid `type`/`summary` is replaced by `❌ 回覆格式錯誤，請再試一次。`; raw model output is never sent.  A JSON
  object with only the safe `type` + `summary` subset is completed with fixed
  defaults before rendering, so an LLM omission cannot break Telegram delivery.
- All other Telegram Bot API calls are passed through unchanged.
