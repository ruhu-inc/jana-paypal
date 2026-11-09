---
id: qwen
version: "0.1"
kind: family_overlay
applies_to: ["*"]
models: [qwen3, qwen3.5]
priority: 60
max_chars: 500
owner: Engine lead
status: draft
---
Reply with the JSON object only, with no text before or after it.
Call tools only through the tool interface, never by writing a call in text.
Keep any thinking short; the answer goes in the JSON fields.
