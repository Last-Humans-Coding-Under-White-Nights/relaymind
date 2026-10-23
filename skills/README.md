# Demo skill packages

One directory per skill. This directory is the default skill root,
`skills/`, a path relative to the working directory, so the gateway
finds it when it starts in the checkout root; started anywhere else,
or installed as a package, it needs configured roots. Operators can
configure an ordered list of local roots instead, which replaces the
default (spec section 4.2). Every subdirectory of a root is
a candidate package, a missing or invalid `SKILL.md` or
`skill.manifest.json` stops the gateway from starting, and skill names
must be unique across roots. See
[`docs/SPECIFICATION.md`](../docs/SPECIFICATION.md) sections 4.2 and 9.

Each package contains:

- `SKILL.md`: Agent Skills `name` and `description` frontmatter, then the
  instruction body with steps, rules, and completion criteria. The
  frontmatter `name` must match the directory name. An `allowed-tools`
  line here grants nothing.
- `skill.manifest.json`: the registry manifest, validated against
  `src/relaymind/skills/schemas/skill_manifest.v1.json`. It holds the
  version, languages, local eligibility, allowed tool IDs, resource
  paths, the completion predicate ID, the optional parallel-workflow ID,
  and optional `prefetch` declarations (see below). Tool IDs, predicate
  IDs, and workflow IDs must be registered in the gateway. `local_eligible: false` marks a skill external-required
  (RM04): its final answer comes from an external model. Declaring a
  parallel-workflow ID only joins the workflow; operator configuration
  must also enable it, and the effective execution mode must admit it
  (spec section 4.4).
- `examples.json`: paired English and Chinese examples. They are
  illustrative, use the sandbox fixture values, and name the tool calls
  they assume. A test runs those calls against the sandbox and checks
  each response with the skill's completion predicate.

| Skill | Tools | Expected route | local_eligible | Completion predicate | Prefetch (`performance.prefetch = true`) |
|-------|-------|----------------|----------------|----------------------|------------------------------------------|
| check-service-health | get_service_health | Local | true | health_status_preserved | get_service_health (service from the request) |
| lookup-latest-deployment | get_latest_deployment | Local | true | deployment_reported | get_latest_deployment (service from the request) |
| summarize-incident-counts | get_incident_counts | Local | true | incident_counts_reported | none: window_minutes is semantic |
| investigate-performance | get_latest_deployment, get_metrics, get_recent_logs | Local prep or escalation, external assessment | false | performance_evidence_separated | get_latest_deployment (service from the request), get_recent_logs (service from the request, limit 50); not get_metrics, whose window is semantic |
| delete-demo-database | list_demo_databases, delete_demo_database | Confirmation required | true | deletion_outcome_reported | none: its tools lead to a confirmation |
| health-and-diagnostic-checklist | get_service_health | Parallel demo workflow, external synthesis | false | health_checklist_separated | none: the fixed parallel workflow (RM15) |

The parallel workflow that health-and-diagnostic-checklist joins runs
in `parallel_demo` (#30, spec section 4.4). The `parallel_workflow`
key keeps its fixed-workflow meaning (RM15). Phase 2 adds a distinct
`workflow` key for a workflow package (RM30); a manifest sets at most
one of these keys, and they are not aliases. Workflow packages under
`workflows/` are not implemented yet, so packages do not use the key
(see `docs/ROADMAP.md`, M9).

## Prefetch declarations (RM29)

A manifest may declare read-only calls that the gateway runs before the
skill's first generation, when the operator sets `[performance] prefetch =
true`. Under the default, `false`, the declarations are only recorded in
`run.accepted` (as `prefetch_declarations`) and nothing runs. The key is
`prefetch`, an array of calls in the order they are declared:

```json
"prefetch": [
  {"tool": "get_latest_deployment", "arguments": {"service": {"from": "request"}}},
  {"tool": "get_recent_logs",
   "arguments": {"service": {"from": "request"}, "limit": {"constant": 50}}}
]
```

Each argument is bound in one of two ways:

- `{"from": "request"}`: an identifier. The parameter must have a closed list
  of allowed values (an `enum` in the tool schema). At run time the gateway
  takes the value from the user turns by the RM05 rule: exactly one allowed
  value must appear there. If none appears, or two do, that call is skipped
  and the model proposes it as before; this is not an error. Only user turns
  count, not the gateway's own questions or any model text.
- `{"constant": value}`: a documented value for a parameter that is not an
  identifier, such as a bounded log `limit`. A constant is never allowed for
  an identifier.

At load the registry refuses the package (the gateway does not start) if a
tool is not read-only, is not in `allowed_tools`, or is not registered; if
a name is not a parameter of the tool; if a required parameter has no
binding; if a request binding names a parameter that is not an identifier
with a closed list; if a constant is given for an identifier; if the same
call is declared twice; if a constant
does not satisfy the tool schema; or if a binding is neither of the two
shapes above. With prefetch on, a skill whose declared calls exceed the
configured `tool_calls` (for a skill that can run locally) or
`run_tool_executions` also stops startup.

A prefetch call is a gateway-made proposal (`tool.proposed` with actor
`gateway`, mechanism `prefetch`, and no model). It goes through the same
checks and the same executor as a model's proposal, counts toward the tool
budgets, and its result enters the conversation as a tool exchange (data,
RM12). When every tool the skill allows has a result, the first skill
generation offers no tools. A later proposal of the same call replays the
recorded operation (RM10).

Changing a package changes its content hash; bump `version` in
`skill.manifest.json` as well (RM07).

The Chinese examples have not been natively reviewed. Chinese support
is best-effort, and spec 0.7 section 9 does not require native review;
the examples are kept, and evidence states that they are not reviewed.
