# Telegram Response Bridge

This service is a transparent Telegram Bot API proxy for Hermes.  Hermes keeps
its existing Telegram adapter, polling, media handling, sessions, and tools.
The bridge only rewrites outgoing `sendMessage` text:

```text
Hermes Telegram adapter -> Response Bridge -> Telegram Bot API
```

Every `sendMessage` text must be a valid Hermes response JSON object.  The
bridge validates it, maps it to a short Telegram message, and never forwards
the JSON or a malformed free-text response to the user.

## Why this boundary

Hermes already supports a Telegram `base_url`.  Pointing that setting to this
service lets the bridge proxy *all* Bot API calls, including `getUpdates` and
file downloads.  This preserves the working inbound path instead of replacing
it with a new webhook implementation.

The bridge has no database, queue, state machine, callback routing, or
confidence-based routing.

## Local verification

```sh
python3 -m unittest discover -s tests -v
```

## Zeabur deployment

1. Create a new service from this directory's Dockerfile.  Add the environment
   variable `TELEGRAM_BOT_TOKEN` with the **existing** bot token.  Do not put
   the token in source code or logs.
2. Obtain the bridge service's private hostname from Zeabur's Networking page.
   It will be similar to `telegram-response-bridge.zeabur.internal`.
3. Add the following to the active Hermes profile configuration, replacing the
   placeholder with that exact hostname.  Keep the existing Telegram token and
   every other Telegram setting unchanged.

   ```yaml
   telegram:
     extra:
       base_url: http://<bridge-private-hostname>/bot
       base_file_url: http://<bridge-private-hostname>/file/bot
   ```

   If the active profile already has `telegram.extra`, merge these two keys;
   do not replace its other keys.
4. Append the contents of
   [`hermes/AGENTS-telegram-json.md`](hermes/AGENTS-telegram-json.md) to the
   active Hermes workspace's existing `AGENTS.md`.  Do not replace unrelated
   workspace instructions.
5. Restart Hermes once.  There must be only one Hermes Telegram poller for the
   bot token.  The bridge proxies that poller; it does not poll Telegram itself.
6. Test `success`, `question`, and `error` in a private Telegram chat.  If the
   result is not correct, remove the two `base_url` settings and restart Hermes
   to return to the original direct Telegram path.

## Health check

`GET /health` returns `{"status":"ok"}` and does not contain configuration
or secrets.

## Security choices

- The bridge accepts only paths containing its configured bot token and only
  forwards them to `https://api.telegram.org` (or an explicitly configured
  test origin).
- Uvicorn access logging is disabled so request URLs cannot reveal the bot
  token.
- A `sendMessage` with free text, malformed JSON, or an invalid contract is
  replaced by `❌ 回覆格式錯誤，請再試一次。`; raw model output is never sent.
- All other Telegram Bot API calls are passed through unchanged.
