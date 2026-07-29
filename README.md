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

## Hermes write plugins

Business writes use small native Hermes plugins. The canonical Todo plugin is
stored at [`hermes/plugins/todo-direct-ingest`](hermes/plugins/todo-direct-ingest).
It adds a native `todo_execute` tool and a Telegram-private-chat
`pre_llm_call` instruction. Hermes interprets natural language into a concise
item, due date, person, and create/complete action; the tool is the only Todo
component allowed to validate and append the Hermes JSONL or update Notion.

This keeps one Telegram entry point and one LLM conversation. The plugin does
not poll Telegram, send its own acknowledgement, invoke a terminal, or ask the
model to write files. It is deployed to the Hermes service's persistent
`/opt/data/plugins` directory. At startup, Zeabur copies only the Todo and
Expense plugin files into Hermes's active `/opt/data/runtime/plugins` home.
The Todo runtime directory remains `00-todo-direct-ingest`; the existing
Expense pre-dispatch route is unchanged and still handles explicit expenses
before the LLM.

This is deliberately separate from the response bridge. The bridge still owns
only final-response rendering, while the existing `expense-direct-ingest`
plugin and the single built-in Telegram poller remain unchanged.

### SI/PI idea plugin

[`hermes/plugins/sipi-idea-ingest`](hermes/plugins/sipi-idea-ingest) is an
additive native plugin. It gives Hermes one `sipi_idea_execute` tool so the LLM
can shorten an explicitly requested SI/PI idea, while the executor performs the
only append-only write to `/opt/data/workspace/sipi-idea-inbox.jsonl`.

- `create` stores the concise title, optional summary, original Telegram text,
  source event id, and `pending` state.
- `consume` appends a `consumed` status event for the single best pending match;
  it does not ask the user to choose candidates.
- The plugin never reads or writes Notion. Todo and Expense routing rules keep
  precedence for actionable work and expenses.
- Deployment uses new persistent and runtime directories. It does not add the
  plugin to the shared Todo/Expense copy list in `/opt/telegram_bridge_init.py`.

### 21:00 daily review

[`hermes/scripts/daily_review.py`](hermes/scripts/daily_review.py) is a
deterministic no-agent cron script. It queries unfinished Notion todos, reads
pending SI/PI ideas, posts one canonical payload to n8n, and prints the same
human-readable report for Hermes's Telegram delivery.

Every todo line contains only `item｜due_date`; the SI/PI section contains only
the still-pending titles. Hermes's Telegram adapter owns the 4,096-character
delivery boundary and splits an oversized report without creating another
poller. The intended schedule is `0 21 * * *` in `Asia/Taipei`.

The n8n source template is
[`n8n/hermes-daily-review.workflow.json`](n8n/hermes-daily-review.workflow.json).
It is a new webhook workflow, isolated from all Watch workflows. It validates a
shared header, deduplicates by `report_id`, sends Gmail, and marks the report
sent only after Gmail succeeds. The workflow must remain inactive until its
shared secret, sender/recipient address, and Gmail SMTP App Password credential
are configured. The SMTP credential uses `smtp.gmail.com`, port `465`, and
SSL/TLS; its App Password must be entered directly in n8n and never committed.

If n8n is unavailable, Telegram delivery continues and the canonical payload is
retained once in `/opt/data/workspace/daily-review-delivery-outbox.jsonl` for a
later retry. The existing morning-focus, Todo-sync, and end-of-day cron jobs are
not modified by this feature.

### Telegram 待辦語意

- 自然語句可直接新增，例如 `今天要詢問歐美亞有關護照申辦的事情`；Hermes 會整理成短項目與期限，不需要再輸入固定前綴。
- 自然語句可直接完成，例如 `已完成歐美亞護照事情`；Hermes 先整理核心項目，executor 再選擇最相近的未完成 Notion 待辦。
- 不詢問候選或二次確認。沒有可信匹配時，建立一筆已完成紀錄，留待每日復盤。
- Telegram 原文保留在 JSONL 與 Notion `備註`；`todo-inbox.jsonl` 維持 append-only。
- 一般聊天、待辦查詢、`/指令` 與支出訊息不使用 Todo 工具。

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
