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

The launcher and latency benchmark have recorded runs on macOS and
native x86-64 Windows (AC15, #110). The 500 ms first model action
hypothesis was missed on both machines (p95 508 ms and 3610 ms). See
[`docs/EVIDENCE.md`](docs/EVIDENCE.md) for the exports and limitations,
and [`docs/RELEASE.md`](docs/RELEASE.md) for the acceptance status.

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
[`models/README.md`](models/README.md). The local model is optional: a
gateway with only an external model works too (see "Without a local
model" below).

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

The limits that bound each run (time, model generations, tool calls)
have defaults. If the local model is slow on your machine, raise them in
the `[budgets]` table; the example file shows the keys, and every run
records the values it used.

Without an external model, a task that needs one ends with a clear
`needs_external` error.

### Without a local model

Leave out the `[local_model]` table and fill in `[external_model]`. The
gateway then picks the skill without a model call (the `example_match`
selector), asks for a missing service name itself, waits for your
approval before a change, and sends every generation to the external
model. Only `auto` and `external_only` can be used. With a local model,
the gateway also checks at startup whether it answers and whether it is
fast enough for the `[budgets]` stage limit; when it is not, one printed
line says so and `auto` uses the external model the same way. Set
`detect = false` in a `[placement]` table to skip that check. Every setting is described in
[`docs/SPECIFICATION.md`](docs/SPECIFICATION.md), section 14.1.

### Loopback HTTP selector

RM28 permits a selector running in another local process. Start the deterministic
standard-library example in another terminal:

```bash
python scripts/example_selector.py --port 8051
```

Configure its exact identity in the launch file:

```toml
[selection]
chain = [{ transport = "http", name = "example_keyword", version = "1", url = "http://127.0.0.1:8051/select" }, "example_match"]
timeout_seconds = 2
```

The gateway sends one redacted [SelectionRequest](src/relaymind/selection/schemas/selection_request.v1.json)
and validates the [SelectionResult](src/relaymind/selection/schemas/selection_result.v1.json).
Only plain `http` loopback endpoints are accepted; credentials, query strings, redirects
and environment proxies are excluded. Configuration needs no selector connection.
Bump the configured version when selector behavior changes. The gateway admits
proposals and retains its permission, identifier, tool and confirmation checks;
a selector never executes a tool or grants permission (RM28).

The keyword example has no model and makes no API calls. It demonstrates the
transport without claiming useful selection accuracy. HTTP attempts and their
nullable model-call self-reports are labeled separately in traces and evaluation;
those reports never verify total external inference cost. The same production
transport can be benchmarked with explicit `--selector-name example_keyword
--selector-version 1`; see [scripts/README.md](scripts/README.md). The
[87-case keyword HTTP capture](docs/EVIDENCE.md#2026-10-24--m8-keyword-http-selector-apple-m1)
records the actual scores, transport timings and limitations separately from the
built-in selector evidence.

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

For the Python example, install the client SDK separately:

```bash
python -m pip install openai
```

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
- **Progress while a streamed request works.** By default the stream
  starts when the run has finished. Set `early_headers = true` in the
  `[performance]` table and a streamed request gets its headers at once,
  with `X-RelayMind-Events-URL` naming the run's event stream (open it with
  the same token to follow the run), then a keep-alive comment while it
  works. No answer text is sent before the answer is ready.
- **Fewer round trips.** In the same `[performance]` table,
  `calls_per_generation = "many"` lets the model ask for several read-only
  tools in one step, and `prefetch = true` runs the read-only calls a skill
  declares (when your request names the service) before the model starts.
  Both are off by default; see `relaymind.example.toml` and
  `skills/README.md`.
- **Warm-up at startup.** Before it prints the address, the gateway sends
  two short requests to the local model so the first real request does not
  pay for loading it. They are not runs. Set `warmup = false` in a
  `[placement]` table to turn it off, or `warmup_seconds` to change the
  30 second limit; a model that does not answer in time is one printed line,
  not a startup error.

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
