# Design rationale: phase 2 (spec 0.31)

Date: 2026-10-23. This document records why spec 0.31 is shaped as it
is: the customer feedback it answers, the evidence it rests on, the
options weighed, and what the reviews removed. It is not normative and
holds no rule, number, or plan of its own:

| What | Its one source |
|------|----------------|
| Behavior, requirements, defaults | [`SPECIFICATION.md`](../SPECIFICATION.md) (RM26 to RM32, AC17 to AC23, section 12.4) |
| Measured and derived numbers | [`EVIDENCE.md`](../EVIDENCE.md), the 2026-10-23 analysis (findings F1 to F9) |
| Milestones, order, issues | [`ROADMAP.md`](../ROADMAP.md) (M7 to M9) and the issues it lists |
| What changed when | [`CHANGELOG.md`](../../CHANGELOG.md) |

## 1 The feedback

1. Some machines are not suited to the local model; on them it is
   slower than the external model. RelayMind assumed local inference
   is fast.
2. Some PCs cannot run a local model at all, and RelayMind required
   one.
3. Customers want workflows, usually in YAML, selected by intent and
   using local and external models where each is available.
4. Customers want to replace the skill, tool, and workflow selection
   with their own system.

Purpose, restated: the best use of an external language model from a
PC with limited resources. Waiting time to the first useful response
and to the result is the objective; external calls and tokens per
completed task are measured beside it.

## 2 What the evidence changed

The first draft assumed the local model's tool choice was the weak
point and that faster routing was the lever. The recorded traces said
otherwise:

- The local model chooses tools well; most local failures come from
  its second answer generation rewriting a draft that was already
  correct (F1). So the cheapest win is publishing the checked draft
  (RM31), not smarter routing.
- On the external path, the gateway's own answer check forces a
  correction round trip on a large share of skill-carrying answers
  (F6), and every external call has a large fixed cost (F3). So the
  levers are telling the external model what will be checked (spec
  section 4.4), admitting several reads per turn (F4, section 4.3), and
  fetching request-determined evidence before the first call (RM29).
- An external routing call would cost more than the local stage it
  replaces (F2, F3). So without a usable local model, selection is
  deterministic (the example matcher, RM28), never an extra round trip.
- Forwarding external text as it arrives would have shown users text
  the gateway later rejected (F5, F6), and the answer is a small part
  of an external run (F7). So streaming is gated per statement and is
  P1 (RM32).
- Per-skill local success measures model quality, not device speed
  (F8). So the expected-wait placement rule is an experiment, and the
  default placement rules use only timing (RM27).
- The cached-token figures were never measurements (F9). So prefix
  reuse is measured before any layout work (section 10).

## 3 Decisions and the alternatives rejected

- **Machines are classified by measurement, not hardware** (RM26,
  RM27). A hardware rule ("old CPU", "DDR3") is a guess that does not
  follow a machine that throttles or a model that gets lighter. The
  gateway measures its own warm-up and generations instead.
- **Detection instead of a configuration flag** (RM26). An operator
  cannot know whether the local model will help; the gateway can
  check at startup and keep checking.
- **Timing samples instead of a calibration command and a profile
  file** (RM27). Samples come free with every generation and follow
  the machine; a one-time profile goes stale.
- **Out-of-process selectors only** (RM28). An in-process plugin could
  read credentials and run tools, so "a selector cannot execute
  anything" would be a promise, not a rule. A loopback HTTP endpoint
  with published schemas makes it enforceable and needs no gateway
  code to implement.
- **Workflows belong to skills** (RM30). Separate catalog entries would
  break the eight-skill router cap and lengthen the prompt on slow
  machines; selecting the skill selects its workflow.
- **The fixed parallel workflow stays code** (RM15, RM30). Aliasing its
  manifest key to the new `workflow` key would have routed a skill
  external whenever its package was disabled.
- **Every new option defaults to the earlier behavior** (sections 12.2
  and 14.1). The AC01 hard-gate test asserts today's answer pass, and a
  performance revision that changes defaults before measuring them
  would contradict section 12. The baseline stays reproducible by
  launch keys.
- **Our own selection benchmarks** (section 12.4). No existing
  benchmark measures selection for this task, so the project builds one
  from two public datasets, SkillsBench and ToolRet-Training-20w, and
  scores every selector with precision, recall, and Jaccard through the
  same interface.
- **external_only is not changed.** It is the comparison arm of the
  corpus and the demo UI; managed external execution is auto without a
  usable local model.

Rejected along the way: a calibration command and a profile file;
runtime slot pinning and a launch-setting change (unmeasured, and a
manifest change forces requalification); an external routing
generation; in-process and standard-input selectors; inline workflows
returned by selectors; a RouteDecision version 2 and a run-state
rename (public API churn); standalone workflow catalog entries; a new
corpus version (a sidecar file keeps version 1 comparable); a
generalized parallel step; exploration on a user's request (an idle
probe instead); a forwarded-then-rejected gate for streaming (zero by
construction).

## 4 Reviews

The design was reviewed three times before the spec was written, and
once more by the owner:

1. A builder check asked eleven planning questions; each is now
   answered by spec text (selection fall-through in RM28, placement per
   generation in RM27, budgets in section 14.1, streaming scope in
   RM32, workflow stages and authority in RM30, selector model calls in
   RM28, caller options under the draft pass in RM31, readiness in
   section 7.4).
2. A re-analysis of the recorded traces produced findings F1 to F7 and
   F9, which reversed the first draft's priorities.
3. A review of the finished revision against the code found a budget
   key that did not exist in the launch file, two defaults that would
   have broken the AC01 gate, the manifest-key collision, finding F8,
   and three miscounts, all corrected at their source.
4. The owner asked for measured machine classes instead of a hardware
   type, detection of machines that cannot run or cannot be helped by
   the local model, a user-facing README, the concrete selector
   contract, the restated purpose, benchmarks built from the two named
   datasets with precision, recall, and Jaccard, and one source of
   truth for every document.

## 5 Left to implementation

Choices the spec leaves open and the issues settle: the timing-sample
retention, the throughput estimator, the example matcher's method and
thresholds, the selector endpoint path, the YAML binding and template
syntax, the generated criteria text per predicate, event field names
beyond those the spec fixes, and the UI layout.
