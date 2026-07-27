# Telegram final-response contract

For every final response intended for Telegram, output one JSON object only.
Do not include Markdown fences, explanations, greetings, or any characters
before or after the JSON object.

Use this exact schema and no additional top-level keys:

```json
{
  "version": "1.0",
  "type": "success | question | confirm | error | progress",
  "action": "",
  "title": "",
  "summary": "",
  "confidence": 0.0,
  "data": {},
  "actions": []
}
```

Rules:

- `type` must be exactly one of `success`, `question`, `confirm`, `error`, or
  `progress`.
- `summary` must be a concise user-facing Traditional-Chinese message.
- For `confirm`, `title` must be a short, non-empty confirmation title.
- `confidence` must be a number from `0.0` to `1.0`.
- `data` must be an object and `actions` must be an array.  They are retained
  for future clients but do not cause Telegram buttons in this version.
- This contract applies even for errors, clarifying questions, progress, and
  simple acknowledgements.
