# OpenClaw round trips on the real pinned host: Linux VPS functional check (#137)

**Linux VPS functional check (not a supported-platform qualification).**
This is one run of the real pinned OpenClaw host, through the harness host
scenarios of `scripts/openclaw_qualification.py`, against the real gateway
over scripted model doubles on a Linux x86-64 VPS. It is not live model
evidence (no llama-server, no live model, no external model), not a
qualification of a supported platform, and not a Mac or Windows result.
macOS and Windows rows for these scenarios stay `not_run`.

## Identity and environment

| Item | Value |
| --- | --- |
| Machine | Linux VPS, `Linux 7.0.0-38-generic x86_64` |
| Host | `OpenClaw 2026.10.7 (157d8c2)`, the pinned `openclaw@2026.10.7` from `integrations/openclaw/node_modules` (`node_modules/.bin/openclaw`) |
| Node, npm | v24.22.1, 11.19.1 |
| Python | 3.14.4 (shared venv) |
| Gateway | the harness's host mode: the real FastAPI app (`ClientToolsChat`) on 127.0.0.1 over the model doubles of `tests/gateway_helpers.py`, one fresh gateway and token per scenario |
| Commit tested | `fd6aca1e7a9c6ac60b6b47d74d23f3e35c51cd1c`, clean (every export's `versions.relaymind_revision` says `dirty: false`) |
| Plugin tarball | `relaymind-openclaw-provider-0.1.0.tgz`, 12 files, npm shasum `d960ca0d8e61bdff53b4bbc8c6fa5ae014ed9bd8` |
| Test host tools | `integrations/openclaw/test/host-tool-plugin/`, installed from the directory |
| Date | 28 October 2026 |

Isolation: every host command ran with `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`
and `OPENCLAW_CONFIG_PATH` pointing into a fresh temp dir, and `agent exec`
had `--config` set to that file. The plugin's `baseUrl` was the literal
`${RELAYMIND_BASE_URL}`, which the host filled from the environment the
harness gives the command. No token was pasted: the only token path was
`RELAYMIND_APP_TOKEN` in that environment (the plugin's declared `envVars`),
and it worked in every scenario. The harness never writes the token.

Reproduce from the repository root (Node on `PATH`, `npm ci` done in
`integrations/openclaw`), a few minutes, no model:

```bash
python docs/evidence/2026-10-28-openclaw-vps-functional-137/scaffold/vps_functional.py <scratch dir>
```

## Setup steps

| # | Step (the command form that worked) | Result |
| --- | --- | --- |
| 1 | `npm run build`, `npm pack --pack-destination <dir>` | exit 0 |
| 2 | `plugins install npm-pack:<tgz> --force --accept-capabilities` | exit 0; installed disabled until configured |
| 3 | `config set plugins.entries.relaymind.config.baseUrl '${RELAYMIND_BASE_URL}'`, `plugins enable relaymind` | exit 0 (the host warns that the variable is missing while it is unset) |
| 4 | `plugins install integrations/openclaw/test/host-tool-plugin --force` | exit 1, as expected: `requires capability consent ... Re-run ... with --accept-capabilities` |
| 5 | `plugins install integrations/openclaw/test/host-tool-plugin --force --accept-capabilities` | exit 0; enabled at install, no `plugins enable` needed (it has no required config) |
| 6 | `plugins inspect relaymind-test-host-tools --runtime --json` | `status: loaded`, `toolNames: ["relaymind_test_echo", "relaymind_test_write"]`, `contracts.tools` the same two, typed hook `before_tool_call`, no diagnostics. This is the Q9 fix (`contracts.tools` in the manifest) on a real host |
| 7 | `config set tools.toolSearch false`, `config set tools.alsoAllow '["relaymind_test_echo","relaymind_test_write"]' --strict-json` | exit 0. Both are needed, see below |
| 8 | `plugins uninstall relaymind-test-host-tools` (no TTY) | exit 1: `requires confirmation input ... pass --force` |
| 9 | `plugins uninstall relaymind-test-host-tools --force` | exit 0: `Removed: plugin settings, install record, directory.` |

Tool exposure (found before the recorded run, with the same host and
config, through the default scenario): a loaded plugin's tools did not
reach the model by themselves. With `tools.toolSearch: false` alone the
host sent 41 direct tools, none of them the test tools (the macOS run saw
the same 41), and the gateway repaired the scripted echo call. With
`tools.alsoAllow` naming the two tools as well, it sent 43, including both,
and the scenario passed. `tools.profile: "full"` instead of `alsoAllow`
also exposed them (58 tools). With tool search on (the host default) and
`alsoAllow` set, it sent 11 tools, and the test tools were again not among
them. Those exploratory runs were not
kept; the recorded run uses `toolSearch: false` and `alsoAllow`.

## Harness scenarios

Each row is one `python scripts/openclaw_qualification.py host --scenario
<name> -- openclaw agent exec --config <cfg> --json --model
relaymind/relaymind-client-tools "<prompt>"` (the exec rows run it through
`sh -c` with `--cwd "$RELAYMIND_TEST_SANDBOX"`). `checks.passed` decides; the
host's `ok` does not.

| Export | `tools.exec.mode` | Host (`ok`, final, toolSummary) | `checks` | Result |
| --- | --- | --- | --- | --- |
| `text-tool.json` (default) | unset | `true`, "The tool said: echo: lantern.", 1 call, 0 failures | proposed `relaymind_test_echo`, gateway tool executions 0, result reached the next run | **passed** |
| `tool-deny.json` | `deny` | `true`, "The host did not allow the write.", 1 call, 1 failure | sandbox 0 of 0 lines, proposed `relaymind_test_write`, executions 0, result reached | **passed** |
| `tool-approve.json` | `full` | `true`, "The note was written.", 1 call, 1 failure | sandbox **0 of 1** lines, proposed `relaymind_test_write`, executions 0, result reached | **failed** (correctly) |
| `exec-deny.json` | `deny` | `true`, "The host did not allow the command.", 1 call, 1 failure | sandbox 0 of 0 lines, proposed `exec`, executions 0, result reached | **passed** |
| `exec-full.json` | `full` | `true`, "The note was written.", 1 call, 0 failures | sandbox 1 of 1 line, proposed `exec`, executions 0, result reached | **passed** |

Each scenario made two gateway runs, both `completed`: the call proposed in
one host request and the host's tool result in the next. Every export's
`runs` list is in store order, not creation order: the run with
`tool.proposed` can come second.

What decided the two write scenarios: the test write tool's
`before_tool_call` hook asks for a plugin approval, and headless `agent exec`
has no approval route. In both runs the host logged
`[tools] relaymind_test_write failed: Plugin approval unavailable:
non-interactive CLI runs have no approval-capable initiating surface.` and
wrote nothing. So `tool-deny` passed because the approval failed closed,
not because of `tools.exec.mode: deny` (that mode governs the host's `exec`
tool only). `tool-approve` cannot pass headless, whatever the exec mode:
the host blocks a plugin approval with no route, as its docs say (no
approval route: the call is blocked). The doubles' scripted closing text
claimed a write that never happened, and the host said `ok: true`; the
sandbox and the checks show the write did not happen. No headless allow
mode for a plugin approval was found. The headless allow path that works is
the host's own `exec` under `tools.exec.mode: full` (`exec-full`, passed);
`exec-deny` logged `exec denied: host=gateway security=deny`.

## Q8: the default scenario at `a52f8bf`

On macOS at `a52f8bf` the default scenario reported `checks.passed: false`
although the host printed the scripted text. The cause was the harness, not
the config-row workaround (which only made the model known): the scenario
scripted an opening text turn before the echo call, so the documented
command, one `agent exec`, ended on that text and could never show a
proposed call. With two execs the second one proposed the echo tool, but
the test tools were not offered (Q9, and the tool exposure above), so the
gateway repaired the call. The scenario now scripts the echo call, then the
closing text, which one host turn covers (`text-tool.json` passed here).

## Q6: the call ID rewrite, seen again

The gateway proposed `call_echo_1`, `call_write_1`, and `call_exec_1`; the
host sent them back as `callecho1`, `callwrite1`, and `callexec1` in every
scenario (`checks.replayed_call_ids`), the same underscore stripping the
macOS probe and the macOS qualification saw. The harness matched each one
through `answers_call`, never by the exact string.

## What this does not show

- Any macOS or Windows result for these scenarios: `not_run`. The earlier
  macOS run at `a52f8bf` failed them for the reasons in Q8 and Q9.
- A plugin approval answered by an operator (allow-once or deny at a
  running OpenClaw Gateway): `not_run`. Here both write scenarios were
  decided by the missing approval route.
- Abort (#138), live model behavior, and request size against the local
  limit: not run here. The doubles do not count tokens.
- The wire: no recording proxy ran, so request headers and bodies are not
  in this export. The gateway's trace is.

## Files

| File | Content |
| --- | --- |
| `steps.json` | every command (sanitized argv), its exit code and expected exit, seconds, stdout and stderr tails |
| `exports/<scenario>.json` | the harness export of each scenario: status, exit code, host stdout and stderr tails, the gateway's runs and events, versions, and `checks` |
| `scaffold/vps_functional.py` | the driver that produced these files |

Sanitizing: the scratch dir became `<scratch>` (the harness's temp dir
`<tmp>`), the checkout `<repo>`, the home dir `<home>`, the hostname
`<hostname>`, and loopback ports `<port>`. A string field in an event
payload over 2,000 characters (the host's system prompt) is kept by its
length only. The harness replaces the token in the host's output and fails
if it reaches an export.
