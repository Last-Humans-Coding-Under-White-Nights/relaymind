# Testing RelayMind (Milestone 1)

How to verify the M1 deliverable by hand on macOS and Windows, next to
the automated suites. M1 qualifies the local model runtime and the
adapters (see [`ROADMAP.md`](ROADMAP.md)); the gateway's own public API
(`/v1/chat/completions`, `/relay/v1/runs`) arrives in M4, so manual
JSON testing in M1 targets the llama-server endpoint that the
`LlamaCppAdapter` talks to.

## Automated suites

```bash
pytest                  # deterministic suite (model doubles, runs in CI)
pytest -m live          # live smoke against a local llama-server
ruff check .
ruff format --check .
```

The live suite needs `llama-server` on PATH and the pinned model
artifact (see [`models/README.md`](../models/README.md)). It starts and
stops its own server; nothing below is required for it.

## Manual JSON testing from a terminal

### 1. Start the pinned runtime

Use the same launch settings the live smoke test pins (spec section 6:
non-thinking is configured through chat-template settings, not prompt
strings).

macOS / Linux (Terminal):

```bash
llama-server -m models/Qwen3-0.6B-Q8_0.gguf \
  --host 127.0.0.1 --port 8033 --ctx-size 8192 \
  --jinja --chat-template-kwargs '{"enable_thinking": false}'
```

Windows (PowerShell 7.3+):

```powershell
llama-server -m models/Qwen3-0.6B-Q8_0.gguf `
  --host 127.0.0.1 --port 8033 --ctx-size 8192 `
  --jinja --chat-template-kwargs '{"enable_thinking": false}'
```

On older PowerShell versions, native-argument quoting mangles the JSON;
use the stop-parsing token and escape the inner quotes by hand, since
the Windows C runtime both splits on the unquoted space and strips
unescaped double quotes:

```powershell
llama-server --% -m models/Qwen3-0.6B-Q8_0.gguf --host 127.0.0.1 --port 8033 --ctx-size 8192 --jinja --chat-template-kwargs "{\"enable_thinking\": false}"
```

Note for all Windows steps below: in Windows PowerShell 5.1, `curl` is
a built-in alias for `Invoke-WebRequest`, so the macOS curl commands
fail if pasted as-is. Type `curl.exe` explicitly (or
`Remove-Item alias:curl`) to use real curl.

### 2. Health check

This is the endpoint `LlamaCppAdapter.health()` maps to
ready / loading / unreachable.

macOS / Linux:

```bash
curl -s http://127.0.0.1:8033/health
```

Windows (PowerShell 7+):

```powershell
Invoke-RestMethod http://127.0.0.1:8033/health -SkipHttpErrorCheck
```

Expect `{"status": "ok"}` once the model is loaded (HTTP 503 means
still loading). Without `-SkipHttpErrorCheck`, PowerShell turns the
503 into a thrown error instead of showing the loading status; on
Windows PowerShell 5.1, which has no such switch, wrap the call in
`try/catch` to inspect the response status.

### 3. Send a chat completion

Checks the answer path and that non-thinking mode holds: the reply must
contain no `<think>` block.

macOS / Linux:

```bash
curl -s http://127.0.0.1:8033/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "stream": false,
    "messages": [
      {"role": "user", "content": "Reply with exactly one word: pong"}
    ]
  }'
```

Windows:

```powershell
$body = @{
    stream   = $false
    messages = @(
        @{ role = "user"; content = "Reply with exactly one word: pong" }
    )
} | ConvertTo-Json -Depth 5

(Invoke-RestMethod -Uri http://127.0.0.1:8033/v1/chat/completions `
    -Method Post -ContentType "application/json; charset=utf-8" `
    -Body $body).choices[0].message.content
```

Selecting `.choices[0].message.content` matters: PowerShell's default
formatting truncates nested objects, which would hide the content you
are checking for a leaked `<think>` block. The `charset=utf-8` matters
too — Windows PowerShell 5.1 otherwise encodes the body as Latin-1,
which mangles the Chinese prompts the bilingual tests use (spec
section 12.2).

(`curl.exe` also ships with Windows 10+; the macOS command works there
too if you keep the JSON in a file and pass `-d @body.json`.)

### 4. Send a tool-call request

Mirrors the live smoke test: the model should propose
`get_service_health` with `{"service": "staging-api"}`.

macOS / Linux:

```bash
curl -s http://127.0.0.1:8033/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "stream": false,
    "messages": [
      {"role": "system", "content": "You check service health. Use the get_service_health tool to answer health questions."},
      {"role": "user", "content": "Is staging-api healthy right now?"}
    ],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_service_health",
        "description": "Return current health for a service.",
        "parameters": {
          "type": "object",
          "properties": {"service": {"enum": ["staging-api", "payments-api"]}},
          "required": ["service"],
          "additionalProperties": false
        }
      }
    }]
  }'
```

Windows:

```powershell
$body = @{
    stream   = $false
    messages = @(
        @{ role = "system"; content = "You check service health. Use the get_service_health tool to answer health questions." }
        @{ role = "user"; content = "Is staging-api healthy right now?" }
    )
    tools = @(
        @{
            type     = "function"
            function = @{
                name        = "get_service_health"
                description = "Return current health for a service."
                parameters  = @{
                    type                 = "object"
                    # 'enum' is quoted: it is a reserved keyword in
                    # PowerShell 5.0+ and can confuse the parser unquoted.
                    properties           = @{ service = @{ 'enum' = @("staging-api", "payments-api") } }
                    required             = @("service")
                    additionalProperties = $false
                }
            }
        }
    )
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri http://127.0.0.1:8033/v1/chat/completions `
    -Method Post -ContentType "application/json; charset=utf-8" -Body $body |
    ConvertTo-Json -Depth 10
```

Expect `finish_reason` `"tool_calls"` and a `tool_calls` entry naming
`get_service_health` with arguments `{"service": "staging-api"}`. Note
that on the wire `function.arguments` is a JSON-encoded **string**
(`"{\"service\": \"staging-api\"}"`), not a nested object — that is
the Chat Completions protocol, not a bug. RelayMind's adapters parse
it before any tool call exists (RM09).

## What manual checks do not replace

Manual terminal checks are for exploration and platform qualification
notes (the spec's platform matrix includes Windows). Evidence for
closing a milestone comes from the recorded automated runs: the
deterministic suite plus the marked live tests (spec section 13, RM16:
a manual poke is not a measured result).
