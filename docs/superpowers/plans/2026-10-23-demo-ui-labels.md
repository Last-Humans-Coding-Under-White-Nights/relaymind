# Demo UI phase 2 labels implementation plan

**Goal:** Continue PR9 (#127) on PR #156; render recorded phase 2 evidence.
**Spec:** `docs/SPECIFICATION.md`, sections 8–9, RM12, RM23, RM25–RM29, RM31.
**Architecture:** Extend the existing trace projection and escaped server rendering.
The page copies each visible run's rendered configuration into the shell banner.
**Tech stack:** Python, FastAPI, plain JavaScript, pytest and Chromium.

## Constraints and review focus

- Old runs show “not recorded”; never infer current defaults or an applied pass.
- An absent local model differs from a model whose identity was not recorded.
- Every route retains its own selector attempts and routing call duration.
- Predictions are labeled separately from actual durations; zero is a measurement.
- All event strings are escaped, including copied shell labels.

## Task 1: Trace fields and rendering

- [x] Add failing tests to `tests/test_demo_ui_trace.py` for recorded options,
  selector attempts, first-token timing, applied pass, historical missing fields,
  predictions and multiple routes. Verify with pytest before implementation.
- [x] Extend `trace_view_from` with `configuration`, `routes`, and
  `answer_pass_applied`; keep `route` for existing consumers. Join routing calls by
  call ID only. Extend `_model_calls` with first-token and placement fields.
- [x] Render escaped configuration, route selection and model timing evidence.
  Preserve gateway prefetch labels; leave a workflow-step placeholder for #138.
- [x] Run trace, render and UI endpoint tests.

## Task 2: Shell banner and browser verification

- [x] Write failing Chromium tests for the banner's selected run configuration,
  including the no-local configuration; historical fields and multiple routes are checked in the trace tests.
- [x] Add a shell configuration area populated from escaped trace fragments by
  `app.js`, with one explicitly identified entry per visible run.
- [x] Run UI browser tests, the full pytest suite, ruff check and format check.
- [x] Update testing documentation and changelog; commit and create PR on #156,
  linked to #127 and milestone M7, without attribution trailers.

## Scope decisions

Placement emission and workflow execution belong to their existing issues.
This reader displays their documented optional fields when present and reports
missing evidence honestly; it does not create predictions or workflow steps.

## Review and verification

Independent review found an unescaped optional prediction and unsupported placement
shapes. Both regressions failed before their fixes and pass afterward. The
full suite, including Chromium, lint and formatting are required before publication.
