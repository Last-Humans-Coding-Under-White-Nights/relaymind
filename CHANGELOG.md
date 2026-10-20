# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Model adapter contract (RM18): capabilities, generate, cancel, and
  health with normalized messages and events (`relaymind.adapters`).
- llama.cpp adapter for Qwen3-0.6B over a llama-server endpoint, with
  non-thinking mode configured through chat-template settings and
  exercised by an opt-in live smoke test (`pytest -m live`, spec
  section 6).
- Model artifact manifest machinery: GGUF metadata hashing,
  `load_manifest`/`verify_artifact`, the manifest generator script, and
  the pinned Qwen3-0.6B Q8_0 manifest (`models/`).
- Manual testing guide for the M1 deliverable on macOS and Windows
  (`docs/TESTING.md`).
- Project scaffold: repository layout, CI, and contributor guides.
- Specification v0.1 (`docs/SPECIFICATION.md`) as the single source of
  truth for the prototype.
- Architecture and roadmap documents mapping spec components to code
  layout and milestones.

### Changed

- Specification 0.2 → 0.3: the artifact manifest records the "runtime
  version and commit" (one composite field) instead of only a "runtime
  commit", matching the implemented `runtime_version` field.

- Specification 0.1 → 0.2: section 6 now allows an operator option to
  enable Qwen3 thinking mode through the same chat-template settings.
  Non-thinking stays the default and the latency-experiment
  configuration; reasoning output never appears as answer content, and
  results from the two modes are never mixed.
