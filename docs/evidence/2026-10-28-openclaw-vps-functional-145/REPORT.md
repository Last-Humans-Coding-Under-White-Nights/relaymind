# OpenClaw release artifact through the real pinned host: Linux VPS functional check (#145)

**Linux VPS functional check (not a supported-platform qualification).**
This is one run of `scripts/openclaw_install_check.py`: the release artifact
of the plugin through the real pinned OpenClaw host's whole lifecycle,
against the real gateway over scripted model doubles, on a Linux x86-64 VPS.
It is not live model evidence (no llama-server, no live model, no external
model), not a qualification of a supported platform, and not a Mac or
Windows result. The macOS and Windows rows of this revision stay `not_run`:
there is no Mac or Windows access, and both are support gaps
(`docs/RELEASE.md`, `docs/EVIDENCE.md`).

## Identity and environment

| Item | Value |
| --- | --- |
| Machine | Linux VPS, Ubuntu 26.04.1 LTS, `Linux 7.0.0-38-generic x86_64` |
| Host | `OpenClaw 2026.10.7 (157d8c2)`, the pinned `openclaw@2026.10.7` from `integrations/openclaw/node_modules` (`node .../openclaw/openclaw.mjs`) |
| Node, npm | v24.22.1 (in the host's range `>=24.22.1 <25`), 11.19.1 |
| Python | 3.14.4 |
| Gateway | the real FastAPI app (`ClientToolsChat`) on 127.0.0.1 over the model doubles of `tests/` (three scripted turns: a text answer, a `read` call, the answer after it) |
| Commit tested | `393a6bfd36b14f21332a27215b1644c2b6fc81cb`, clean (`versions.relaymind_revision.dirty` is false) |
| Artifact | `relaymind-openclaw-provider-0.1.0.tgz`, 39,846 bytes, sha256 `4c11884a368eb360d9d6ecab1da3157428917fb26abe38f2ac7e9bc471dcae6d`, built at that commit with `npm run pack:release` (`SHA256SUMS`, `release.json` here) |
| Export | `install-check.json`, sha256 `5d147e6f40ff9c622b17c1fb5cc5a50f0992f19fb377464b040085e9eb84ee8b`, after the path redaction below (as written by the run: `618033af2d68c39296f4d2f9cd17054686e53a89f04d178eb80b0f57bc5be228`) |
| Date | 28 October 2026 |

Isolation: every host command ran with `OPENCLAW_HOME`, `OPENCLAW_STATE_DIR`
and `OPENCLAW_CONFIG_PATH` in a fresh temp dir, with no `OPENCLAW_*` setting
or proxy from the shell, and with `gateway.port` set in that config to a
closed loopback port (57867). The token went in only through
`paste-api-key` on standard input. It is never in an argument, the host's
environment, or the export.

Reproduce from the repository root (Node on `PATH`, `npm ci` done in
`integrations/openclaw`). It takes about three minutes and needs no model:

```bash
(cd integrations/openclaw && npm run pack:release -- <dir>)
python scripts/openclaw_install_check.py \
  --tarball <dir>/relaymind-openclaw-provider-0.1.0.tgz --out <export.json> \
  --label "Linux VPS functional check (not a supported-platform qualification): ..." \
  -- node integrations/openclaw/node_modules/openclaw/openclaw.mjs
```

## Steps (all passed, 166 s in total)

| Step | Host command | Result |
| --- | --- | --- |
| version | `--version` | `OpenClaw 2026.10.7 (157d8c2)` |
| isolate-log, isolate-gateway | `config set logging.file ...`, `config set gateway.port 57867` | exit 0 |
| install | `plugins install npm-pack:<artifact> --force --accept-capabilities` | exit 0, 28.6 s |
| configure, enable | `config set plugins.entries.relaymind.config.baseUrl ...`, `plugins enable relaymind` | exit 0 |
| inspect | `plugins inspect relaymind --runtime --json` | `loaded`, enabled, 0.1.0, provider `relaymind`, no diagnostics |
| store-token | `models auth paste-api-key --provider relaymind` (stdin) | profile `relaymind:manual` |
| discover | `models list --refresh --provider relaymind --json` | `relaymind/relaymind-client-tools` ("Gateway is not running. Refreshing the local model catalog.") |
| text-exchange | `agent exec "Say hello" --model relaymind/relaymind-client-tools --json` | one gateway run, completed; "Hello from RelayMind." in the host's output |
| tool-exchange | `agent exec "Read notes.txt and report its first line." ...` | the gateway proposed `read` (`call_read_1`), the host ran it once and replayed the ID as `callread1`; the next run got the file's text; the host printed the gateway's answer |
| disable | `plugins disable relaymind` | exit 0 |
| uninstall | `plugins uninstall relaymind --force` | exit 0 |
| remove-config | `config unset plugins.entries.relaymind` | exit 0 |
| remove-credential | `models auth logout relaymind:manual --yes` | exit 0 |

There were three gateway runs, all `completed`. The gateway executed no
host tool. The host's `"ok": true` decided nothing: the trace and the
sandbox did.

What uninstall left, and what the guide's removal steps cleared (Q12 of the
macOS qualification, seen again here):

| | config entry | `relaymind:manual` profile | token in a host file |
| --- | --- | --- | --- |
| after `plugins uninstall --force` | present | present | present |
| after `config unset` and `models auth logout` | gone | gone | gone |

## Found and fixed on this branch before the recorded run

The first run at the restacked tip `4796298` failed `discover`, and the two
exchanges were then `not_run`. That run set `OPENCLAW_GATEWAY_PORT` in the
host's environment, a code review fix that had not yet run on a real host.
The pinned host took that port as an explicit Gateway target, so
`models list --refresh` exited 1 with "gateway models.list requires
credentials before opening a websocket" and did not refresh the local
catalog. A bare check in a fresh host home showed the same thing. With the
variable set, the command fails. Without it, or with `gateway.port` in the
config instead, it refreshes. (The host's `list` command skips the local
path when the variable is set.) The fix is commit `393a6bf`, test first in
`tests/test_openclaw_install_check.py`. The stub host now refuses the same
way, and the check sets the closed port with `config set gateway.port`
before the install. The removal steps still ran and passed on that first
run.

## Redacted after the run

The host prints a path under the user's home in its home-relative form, a
tilde for the home. The run's sanitizer replaced only the absolute forms, so three
host prompts in the export kept the checkout's place under the home in
their skill `<location>` entries (PR #182 review). Those were replaced
after the run with the fixed sanitizer of `scripts/openclaw_install_check.py`
(the checkout's home-relative form becomes `<repo>`, any other
home-relative path starts with `<home>`). Nothing else in the export changed. The same three entries of
`docs/evidence/2026-10-27-openclaw-install-check-linux-vps.json` were
replaced the same way, and its host home path became `<host-home>`.

## Also seen

- `paste-api-key` and `models auth logout` warn that "the local Gateway
  could not refresh them". No OpenClaw Gateway was running, and
  `models list` and `agent exec` still used the stored credential.
- `config set ...baseUrl` before `plugins enable` warns "plugin disabled
  ... but config is present". The warning is harmless, and the guide uses
  the same order.
- As in the earlier Linux VPS run, the host also wrote its daily log under
  `/tmp/openclaw/`, outside the isolation.

## Not shown

- macOS at this revision: `not_run` (no Mac access). The earlier, partial
  result is the macOS qualification at `a52f8bf` (branch
  `devnull.exile/m9-mac-qual`, `b575c01`).
- Windows, native and WSL2: `not_run` (no Windows machine). OpenClaw
  2026.10.7 documents native Windows support, with WSL2 optional.
- The stale-token reconnect, a host update or rollback over an existing
  install, and any live model.
