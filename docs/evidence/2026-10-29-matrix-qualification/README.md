# Matrix source qualification, 2026-10-29 (#188)

RM07, RM12, RM16, RM25; specification 0.50 sections 8, 12.4 and 13.
This is bounded source qualification for #187, with no product change.

The supplied skill has a documented, repeatable ZIP download. The proposed
100 original Chinese skills and 25 equivalent-function English pairs remain
**unqualified**. Scheduled refresh also remains unqualified. A public download
and one Chinese body do not establish the cohort, redistribution rights, or
permission to automate a market inventory.

## Observed source and capture

The [supplied detail page](https://matrix.openharmony.cn/#/skillSquare/details?id=69e1dea2c4357076d28ecb48)
loaded in Chromium and identified `hmos-arkui-develop-skill`, version `1.1.1`,
publisher HarmonyOS, updated `2026-10-08`. Its linked upstream is
[HarmonyOS_Skills/harmonyos-agent-skills](https://gitcode.com/HarmonyOS_Skills/harmonyos-agent-skills),
subdirectory `release/02-development/arkui/hmos-arkui-develop-skill`.
An upstream commit was not obtained. These are source observations, not an
assertion that a mutable branch is a pinned revision.

The page links a [Matrix installation guide](https://matrix.openharmony.cn/api/registry/skill/matrix_skill_guide.md).
That guide explicitly documents a direct ZIP download, without requiring a CLI.
Only the download was used. No installer, extraction command, refresh command,
or acquired skill instruction was executed. The guide redirected to
`https://openharmony-matrix.obs.cn-north-4.myhuaweicloud.com/doc/matrix_skill_guide.md`.
The page's other, plural `/api/registry/skills/hmos-arkui-develop-skill` link
returned HTTP 404; it was not used as the acquisition contract.

Two downloads, at `16:23:49.678842Z` and `16:24:42.082100Z`, each returned
99,960 bytes, SHA-256
`b11b85a8aecfb4c0743b254de9bd98183cd218f6cf988731ffa5d46393fa15e1`.
Both resolved to the versioned OBS URL in [report.json](report.json).
The ZIP central directory contains 38 entries, 246,741 uncompressed bytes.
A bounded in-memory reading of `SKILL.md` found a Chinese body and English
frontmatter description. Its 7,496 original bytes hash to
`1b2b281319c6bb9a175e4ec2d9d2d1b05db3ec3e94f761fe88f15d8722f6492c`.
Archive members were never written to disk or executed.

Original ZIPs and page/document captures remain in temporary local storage,
separate from these normalized metadata files. They are not checked in.
[Source observation hashes](report.json) distinguish HTTP response bytes from
rendered UTF-8 body text; a rendered-text hash is not an original archive hash.

## Counts and overlap

The [market page](https://matrix.openharmony.cn/#/skillSquare) defaulted to HMOS,
reported 56 skills, and rendered 40 distinct card names. Its OpenHarmony and
HarmonyOS PC tag counters displayed 98 and 3,031. These are changing,
filter-specific UI counts. They cannot be added, treated as unique assets, or
counted as original Chinese skills. Some visible descriptions were English.
Only **one distinct skill** was captured and inspected for body language.
No bulk crawl was attempted.

The captured skill concerns ArkTS/ArkUI code. The current English SkillsBench
catalog has 195 skills and 87 cases. A diagnostic substring search of catalog
names and descriptions found no `ArkUI`, `ArkTS`, or `HarmonyOS` and four `web`
matches. This is evidence of a domain mismatch to investigate, not a semantic
equivalence test. Generic coding/UI overlap does not establish the required
25 equivalent-function pairs. No pair or 100-skill cohort was qualified, and
no translated or generated substitute was introduced.

## Rights, NOTICE and refresh constraints

The [Matrix terms](https://matrix.openharmony.cn/#/docs/main?type=使用协议)
display v1.0.0, effective 2025-08-22. Sections 5.2 and 6.4 refer to each
publisher's license; attachment 1 section 5 describes free download and use.
Section 2.2 was also reviewed. Sections 2.3, 4.2, 5.1 and 9.2 also require
attention for region, access, platform material and service sharing. These observations do not establish
permission to redistribute assets or automate collection.

No LICENSE, NOTICE, COPYING or COPYRIGHT filename was present in the sampled
archive. The linked upstream's license was not obtained. The asset license
therefore remains **unknown**, rather than an inferred permissive license.
Any later permitted export must retain authorship, upstream URL, exact version
or commit, license text, existing notices and the applicable attribution
requirements. A NOTICE entry must record the Matrix source ID, asset ID,
capture UTC and original hash. Missing rights evidence blocks redistribution;
platform terms never substitute for an asset license.

The documented per-name download is technically repeatable at this observation.
It resolves the current version and is not a qualified bulk catalog, immutable
version API, change feed, or scheduled-refresh permission. No schedule or
production importer was created. The source/rights and inventory gaps must be
resolved before treating a configured source as eligible for scheduled refresh.

## Reproduce the bounded capture and integrity check

Run from the repository root with Python 3.12+ and curl. Keep the capture outside
the checkout and outside every agent skill directory. This downloads opaque
bytes only; it does not install the asset.

```bash
mkdir -p /tmp/relaymind-matrix-capture
curl --fail --show-error --silent --location --max-time 45 \
  --max-filesize 10485760 \
  'https://matrix.openharmony.cn/api/registry/skill/hmos-arkui-develop-skill/install?format=zip' \
  --output /tmp/relaymind-matrix-capture/relaymind-matrix-hmos-arkui-develop-skill-1.zip
python scripts/probes/qualify_matrix.py \
  --manifest docs/evidence/2026-10-29-matrix-qualification/capture-manifest.json \
  --capture-root /tmp/relaymind-matrix-capture \
  --output /tmp/relaymind-matrix-integrity.json
```

Repeat the download and probe into separate output files, then compare hashes
and reports. The historical manifest intentionally rejects changed bytes;
retain the old capture and create a new manifest after reviewing a changed
version. To reproduce the historical archive rather than the latest redirect,
the observed versioned URL is recorded in `report.json`. A new acquisition
needs its own capture time; the checked-in manifest describes this run only.
The two offline derivations in this run were identical to
[integrity.json](integrity.json).

[capture-manifest.json](capture-manifest.json) is a probe-only operator format,
not a claimed Matrix export schema. It carries a versioned envelope and an
`assets` array. Each row requires source ID, asset ID, name, observed version,
UTC capture time, source/download URLs, relative original path and SHA-256;
license and attribution may be supplied. The probe verifies hashes and duplicate
identities, bounds metadata to 1 MiB, assets to 200, each file to 10 MiB and the
capture to 50 MiB. It refuses paths outside the root and output aliases of an
input. It never opens a ZIP member, so ZIP traversal and decompression bombs
cannot cause extraction or decompression in this probe. It verifies byte
integrity, not ZIP validity, licensing or the truth of operator metadata.

The same format can accompany an operator-supplied export from this Matrix
source. Supply original versioned archives, source/capture identities and
rights evidence; preserve original bytes independently of retrieval metadata.
For the proposed cohort, also supply a distinct-ID inventory, source-language
review and reviewed equivalent-function English pairs. Obtain source permission
for the intended refresh cadence and retain its terms/version evidence.
Without that export/permission, keep the lane blocked or revise the cohort in
#187. The qualification does not silently substitute another market or language.

## Environment and exclusions

Linux x86_64, Python 3.14.4, curl 8.18.0, Playwright 1.64.1, Chromium
156.0.8078.4; base checkout `ba4b33a`, probe tree dirty during capture analysis.
The initial browser launch lacked `libgbm.so.1` and `libasound.so.2`; local
Ubuntu library extraction supplied those dependencies without repository changes.
The browser then loaded the supplied detail and terms successfully. Initial
web-reader timeouts and the 517-byte SPA shell were not treated as source absence.

No login, private API client, bulk scrape, asset installation/execution,
production dependency, model call or benchmark run occurred. Source content
was treated as untrusted data. `report.json` records pass/not_run per check;
this qualification is not an M10 cohort or scheduled-refresh pass.
