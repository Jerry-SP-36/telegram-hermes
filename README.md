# Telegram Response Bridge

This service owns the response-rendering boundary for Hermes. Hermes keeps its
existing Telegram adapter, polling, media handling, sessions, and tools. Its
Telegram sender calls the bridge's private `/render` endpoint before using its
existing sender, so only outgoing response text is transformed:

```text
Telegram -> Hermes final reply -> Response Bridge /render -> Telegram sender -> Telegram
```

Contract JSON remains the preferred response format: the bridge extracts and
validates it, then maps it to a concise Telegram message.  Built-in Hermes
commands and general assistant replies may produce normal text instead, so the
bridge now passes bounded plain text through safely. Empty or malformed
JSON-like output still fails closed.

## Why this boundary

The bridge retains a restricted Bot API proxy route as a compatibility fallback
for Hermes versions that honour a custom Telegram `base_url`. The primary path
uses `/render`, which preserves the working inbound path and changes only the
last Telegram-sender step.

The bridge has no database, queue, state machine, callback routing, or
confidence-based routing.

## Deterministic Hermes write plugins

Business writes that must succeed independently of the LLM belong in native
Hermes `pre_gateway_dispatch` plugins. The canonical todo plugin is stored at
[`hermes/plugins/todo-direct-ingest`](hermes/plugins/todo-direct-ingest): it
validates and appends the Hermes todo JSONL, synchronizes Notion, sends a
human-readable acknowledgement, and returns `skip` before the LLM runs. It is
deployed to the Hermes service's persistent `/opt/data/plugins` directory.
At startup, Zeabur copies only the Todo and Expense plugin files into Hermes's
active `/opt/data/runtime/plugins` home. The Todo runtime directory is named
`00-todo-direct-ingest` so Hermes registers its `pre_gateway_dispatch` hook
before Expense without changing either plugin's manifest or business logic.

This is deliberately separate from the response bridge. The bridge still owns
only final-response rendering, while the existing `expense-direct-ingest`
plugin and the single built-in Telegram poller remain unchanged.

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
- A valid JSON contract is rendered with the expected status icon. Normal
  Hermes final text has NUL characters removed and is bounded to 4,000
  characters before delivery. Empty or malformed JSON-like output is replaced
  by `❌ 回覆格式錯誤，請再試一次。`.
- A JSON object with only the safe `type` + `summary` subset is completed with
  fixed defaults before rendering, so an LLM omission cannot break delivery.
- All other Telegram Bot API calls are passed through unchanged.
