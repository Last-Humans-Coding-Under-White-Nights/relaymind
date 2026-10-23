# RelayMind

RelayMind helps a PC with limited resources get the most out of an
external language model. Simple tasks run on a small local model when
your machine can do them quickly; everything else goes to the external
model with as few calls as possible. Work already done is kept when a
task switches over, nothing destructive runs without your
confirmation, and every decision is shown in a trace.

It speaks the OpenAI Chat Completions API, so any OpenAI client can use
it.

## What it does

- **Skills and tools.** A skill is a small instruction package that
  names the tools it may use. The gateway checks every tool call before
  it runs and never guesses an identifier such as a service name.
- **Local first, external when needed.** When the local model cannot
  finish, the request, the tool results so far, and the remaining
  budget go to the external model. Nothing is redone or invented.
- **Confirmation before changes.** An action that changes something
  waits for a one-time approval of that exact call.
- **A trace of every run.** Which model answered, which skill and tools
  were used, each call's timing, and why work was handed over.

This version ships demo skills and tools that work on a sandbox
(two fake services and a few fake databases).

## Install

You need Python 3.12 or newer on macOS (Apple Silicon) or Windows
(x86-64). No WSL or Docker.

```bash
git clone <repo-url> && cd relaymind
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
```

Then install the local runtime (llama.cpp) and download the model
(Qwen3-0.6B, about 0.6 GB) as described in
[`models/README.md`](models/README.md).

## Configure

Copy [`relaymind.example.toml`](relaymind.example.toml) to
`relaymind.toml`. The example works as it is for the local model and
the demo skills. To add an external model, fill in the
`[external_model]` table (base URL, model name, context limit) and put
the API key in an environment variable, never in the file:

```bash
export RELAYMIND_EXTERNAL_API_KEY=...        # macOS
$env:RELAYMIND_EXTERNAL_API_KEY = "..."      # Windows PowerShell
```

Without an external model, a task that needs one ends with a clear
`needs_external` error. Every setting is described in
[`docs/SPECIFICATION.md`](docs/SPECIFICATION.md), section 14.1.

## Start

Start the local runtime as
[`models/README.md`](models/README.md#start-the-runtime) shows, then
the gateway:

```bash
relaymind serve --config relaymind.toml
```

The gateway checks the configuration first and stops with one clear
message if something is wrong. Then it prints its address and a
one-time token:

```
Serving on http://127.0.0.1:8080
Application token: <token>
```

Keep the token in `RELAYMIND_TOKEN`; every request needs it.

## Use

Use the model name `relaymind`, which means "route it for me":

```python
import os
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key=os.environ["RELAYMIND_TOKEN"])
reply = client.chat.completions.create(
    model="relaymind",
    messages=[{"role": "user", "content": "Is staging-api healthy right now?"}],
)
print(reply.choices[0].message.content)
```

Or with curl:

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer $RELAYMIND_TOKEN" -H "Content-Type: application/json" \
  -d '{"model": "relaymind", "messages": [{"role": "user", "content": "Is staging-api healthy right now?"}]}'
```

- **Choose where it runs** with the header `X-RelayMind-Execution-Mode`:
  `auto` (default), `local_only`, or `external_only`.
- **Missing details** come back as a question; send a new request with
  the answer.
- **Changes** wait for approval in the trace UI; a "yes" in the chat
  is not an approval.
- **See every run** at `http://127.0.0.1:8080/ui` (enter the token).
  The `X-RelayMind-Run-ID` response header names the run.

## Coming next

What is planned is in [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Contributing

Read [`AGENTS.md`](AGENTS.md) first: the specification
([`docs/SPECIFICATION.md`](docs/SPECIFICATION.md)) is the source of
truth, and tests come before code.

```bash
pip install -e ".[dev]" && playwright install chromium
pytest && ruff check . && ruff format --check .
```

## License

MIT. See [LICENSE](LICENSE).
