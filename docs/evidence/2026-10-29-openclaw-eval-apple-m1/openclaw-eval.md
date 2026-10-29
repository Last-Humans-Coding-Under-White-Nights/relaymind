# OpenClaw integration evaluation

Spec 0.50 section 12.5. Counts are exact over every attempt, failed, timed-out, and retried ones included. Success is judged from the host's exit status, its final answer, its tool calls, the sandbox, and the gateway trace; the host's `ok` is never a success. A host failover (the host answered through another provider or model) is counted apart and is never an arm success. Completion latency is the runner's clock around the host command (host startup and host tool time included); first useful content is the gateway's clock; the two are never subtracted. Tokens are per model, summed only over calls that reported a count, with unknown calls beside the sum; host-reported usage is never added to them. Chinese results are best-effort and reported apart. 0 local turns is a result, reported with the reasons the runs left the local stage; a local-inference comparison whose RelayMind arm had no local turn compares no local inference. A tool call that matches none of the task's expected calls is counted (unexpected host tools in every arm, unexpected proposals in RelayMind), never a failure, so both arms of a comparison share one success rule.

| Arm | Language | Successes | Host failovers | Timeouts | Interrupts | ok but failed | Gateway runs | Inference attempts | Errors | needs_external | context_limit_exceeded | Local turns | External calls | Unexpected host tools | Unexpected proposals | Completion p50/p95 ms (successful) | First useful p50/p95 ms (gateway) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| direct | en | 18/18 | 0 | 0 | 0 | 0 | unknown | unknown | unknown | 0 | 0 | unknown | unknown | 4 | unknown | 17293/36867 (n=18) | unknown/unknown (n=0) |
| direct | zh (best-effort) | 18/18 | 0 | 0 | 0 | 0 | unknown | unknown | unknown | 0 | 0 | unknown | unknown | 2 | unknown | 18210/33247 (n=18) | unknown/unknown (n=0) |
| relaymind_external_only | en | 18/18 | 0 | 0 | 0 | 0 | 32 | 32 | 0 | 0 | 0 | 0 | 32 | 0 | 0 | 17152/25039 (n=18) | 3972/9317 (n=18) |
| relaymind_external_only | zh (best-effort) | 18/18 | 0 | 0 | 0 | 0 | 33 | 33 | 0 | 0 | 0 | 0 | 33 | 1 | 1 | 18310/27237 (n=18) | 3202/10031 (n=18) |
| relaymind_auto | en | 18/18 | 0 | 0 | 0 | 0 | 35 | 35 | 0 | 0 | 0 | 0 | 35 | 4 | 4 | 17297/40454 (n=18) | 4025/20798 (n=18) |
| relaymind_auto | zh (best-effort) | 18/18 | 0 | 0 | 0 | 0 | 35 | 35 | 0 | 0 | 0 | 0 | 35 | 3 | 3 | 17808/34008 (n=18) | 3662/9813 (n=18) |
| relaymind_no_local | en | 18/18 | 0 | 0 | 0 | 0 | 36 | 36 | 0 | 0 | 0 | 0 | 36 | 4 | 4 | 16492/33545 (n=18) | 3445/10515 (n=18) |
| relaymind_no_local | zh (best-effort) | 18/18 | 0 | 0 | 0 | 0 | 35 | 35 | 0 | 0 | 0 | 0 | 35 | 4 | 4 | 18420/33591 (n=18) | 3598/9666 (n=18) |

## Local stage (gateway runs, all attempts)

- relaymind_external_only, en: 0 local turns of 32 gateway runs, 0 local calls; left the local stage by reason: execution_mode: 32
- relaymind_external_only, zh: 0 local turns of 33 gateway runs, 0 local calls; left the local stage by reason: execution_mode: 33
- relaymind_auto, en: 0 local turns of 35 gateway runs, 0 local calls; left the local stage by reason: context_limit: 35
- relaymind_auto, zh: 0 local turns of 35 gateway runs, 0 local calls; left the local stage by reason: context_limit: 35
- relaymind_no_local, en: 0 local turns of 36 gateway runs, 0 local calls; left the local stage by reason: no_local_model: 36
- relaymind_no_local, zh: 0 local turns of 35 gateway runs, 0 local calls; left the local stage by reason: no_local_model: 35

## Tokens per model (gateway usage, successful attempts)

- direct, en: none recorded
- direct, zh: none recorded
- relaymind_external_only, en: glm-5.3-flash: 32 calls, in 291069 (+0 unknown), out 3326 (+0 unknown)
- relaymind_external_only, zh: glm-5.3-flash: 33 calls, in 300328 (+0 unknown), out 3043 (+0 unknown)
- relaymind_auto, en: glm-5.3-flash: 35 calls, in 318196 (+0 unknown), out 3245 (+0 unknown)
- relaymind_auto, zh: glm-5.3-flash: 35 calls, in 318673 (+0 unknown), out 4054 (+0 unknown)
- relaymind_no_local, en: glm-5.3-flash: 36 calls, in 327972 (+0 unknown), out 3862 (+0 unknown)
- relaymind_no_local, zh: glm-5.3-flash: 35 calls, in 318678 (+0 unknown), out 3334 (+0 unknown)

## Paired comparisons (task and repeat pairs both arms completed)

- integration_overhead (relaymind_external_only against direct), en: 18 pairs, median completion difference -732 ms; successes 18/18 against 18/18
- integration_overhead (relaymind_external_only against direct), zh: 18 pairs, median completion difference -55 ms; successes 18/18 against 18/18
- local_inference_auto (relaymind_auto against relaymind_external_only), en: 18 pairs, median completion difference 437 ms; successes 18/18 against 18/18; no local turn in relaymind_auto: compares no local inference
- local_inference_auto (relaymind_auto against relaymind_external_only), zh: 18 pairs, median completion difference -39 ms; successes 18/18 against 18/18; no local turn in relaymind_auto: compares no local inference
- local_inference_no_local (relaymind_no_local against relaymind_external_only), en: 18 pairs, median completion difference -344 ms; successes 18/18 against 18/18; no local turn in relaymind_no_local: compares no local inference
- local_inference_no_local (relaymind_no_local against relaymind_external_only), zh: 18 pairs, median completion difference 869 ms; successes 18/18 against 18/18; no local turn in relaymind_no_local: compares no local inference

Evidence: live models through a real OpenClaw host (section 12.5); only arms marked ran are evidence. Machine: {"os": "Darwin", "os_version": "macOS 27.0.1", "platform": "macOS-27.0.1-arm64-arm-64bit-Mach-O", "machine": "arm64", "cpu": "Apple M1", "cpu_count": 8, "ram_bytes": 8589934592, "gpu": [{"model": "Apple M1", "cores": "7"}], "python": "3.14.9"}
