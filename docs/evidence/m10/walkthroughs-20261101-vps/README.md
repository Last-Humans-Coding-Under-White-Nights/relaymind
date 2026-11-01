# Bilingual walkthrough manifests (owner VPS, 1 November 2026)

Recorded with `integrations/openclaw/test/host-walkthrough/record.py record --font-dir …`
on the owner's Ubuntu 26.04 x86-64 VPS (spec 0.66 role `linux_host`) against the
pinned OpenClaw 2026.10.7 host, at commit e481432. These files are the metadata
of that run only:

- `manifest.json`: every recorded file with SHA-256 and size, the harness and
  recorder source digests, and the two Noto Sans CJK SC fonts used (from the
  notofonts/noto-cjk `Sans2.004` tag, checked against its git blob hashes).
- `<lang>/capture.json`: the qualification capture of that language's run.
- `<lang>/screens.json`: the step-to-screenshot index.

Result: both `en-US` and `zh-CN` are `complete: true`. The steps open,
recommend, choose, correct, inspect, delete, reset and select_skill are
`recorded`. Loaded skill use, MCP use and MCP approval are `blocked` with
their fixed reasons. Loaded skill use is blocked because it is unqualified
(`skill_loaded_use_unqualified`), not as a host limitation: the pending negative
record that called it one was withdrawn (#267), and its qualification attempt is
#280. MCP use has a pending negative record (`exact_route_absent`) that lifts
nothing until it is approved.

The screenshots and videos (about 17 MB) are not committed. They stay with
the owner, bound by the digests in `manifest.json`. A privacy review of all 44
screenshots and sampled video frames found no IP address, hostname, user name
or token. The work directory is a neutral `/tmp/rm-walkthrough` path, and the
accounts shown are disposable fixture accounts. Known UI gaps seen in these
runs are tracked in #270: English labels in the zh-CN detail block, escaped
path characters, and a wide JSON line.

The privacy statement above covers the screenshots and sampled video frames.
Both committed `capture.json` files also contain the VPS's private RFC1918
address in bind, peer and proxy metadata. That internal address is retained
because the captures are bound by `manifest.json`; changing it would change
the recorded evidence. It is not a public address or a credential. The
capture metadata was not covered by the visual privacy review described above.
